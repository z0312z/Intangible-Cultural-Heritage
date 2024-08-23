import asyncio
import json
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import yaml
from lmdeploy.serve.openai.api_client import APIClient
from loguru import logger
from pydantic import BaseModel
from tqdm import tqdm

from ..tts.tools import SYMBOL_SPLITS, make_text_chunk
from ..web_configs import API_CONFIG, WEB_CONFIGS
from .modules.agent.agent_worker import get_agent_result
from .modules.rag.rag_worker import RAG_RETRIEVER, build_rag_prompt
from .queue_thread import DIGITAL_HUMAN_QUENE, TTS_TEXT_QUENE
from .server_info import SERVER_PLUGINS_INFO


class ChatGenConfig(BaseModel):
    # LLM 推理配置
    top_p: float = 0.8
    temperature: float = 0.7
    repetition_penalty: float = 1.005


class ProductInfo(BaseModel):
    name: str
    heighlights: str
    introduce: str  # 生成商品文案 prompt

    image_path: str
    departure_place: str
    delivery_company_name: str


class PluginsInfo(BaseModel):
    rag: bool = True
    agent: bool = True
    tts: bool = True
    digital_human: bool = True


class ChatItem(BaseModel):
    user_id: str  # User 识别号，用于区分不用的用户调用
    request_id: str  # 请求 ID，用于生成 TTS & 数字人
    prompt: List[Dict[str, str]]  # 本次的 prompt
    product_info: ProductInfo  # 商品信息
    plugins: PluginsInfo = PluginsInfo()  # 插件信息
    chat_config: ChatGenConfig = ChatGenConfig()


# 加载 LLM 模型
LLM_MODEL_HANDLER = APIClient(API_CONFIG.LLM_URL)


async def streamer_sales_process(chat_item: ChatItem):

    # ====================== Agent ======================
    # 调取 Agent
    agent_response = ""
    if chat_item.plugins.agent and SERVER_PLUGINS_INFO.agent_enabled:
        GENERATE_AGENT_TEMPLATE = (
            "这是网上获取到的信息：“{}”\n 客户的问题：“{}” \n 请认真阅读信息并运用你的性格进行解答。"  # Agent prompt 模板
        )
        input_prompt = chat_item.prompt[-1]["content"]
        agent_response = get_agent_result(
            LLM_MODEL_HANDLER, input_prompt, chat_item.product_info.departure_place, chat_item.product_info.delivery_company_name
        )
        if agent_response != "":
            agent_response = GENERATE_AGENT_TEMPLATE.format(agent_response, input_prompt)
            print(f"Agent response: {agent_response}")
            chat_item.prompt[-1]["content"] = agent_response

    # ====================== RAG ======================
    # 调取 rag
    if chat_item.plugins.rag and agent_response == "":
        # 如果 Agent 没有执行，则使用 RAG 查询数据库
        rag_prompt = chat_item.prompt[-1]["content"]
        prompt_pro = build_rag_prompt(RAG_RETRIEVER, chat_item.product_info.name, rag_prompt)

        if prompt_pro != "":
            chat_item.prompt[-1]["content"] = prompt_pro

    # llm 推理流返回
    logger.info(chat_item.prompt)

    current_predict = ""
    idx = 0
    last_text_index = 0
    sentence_id = 0
    model_name = LLM_MODEL_HANDLER.available_models[0]
    for item in LLM_MODEL_HANDLER.chat_completions_v1(model=model_name, messages=chat_item.prompt, stream=True):
        logger.debug(f"LLM predict: {item}")
        if "content" not in item["choices"][0]["delta"]:
            continue
        current_res = item["choices"][0]["delta"]["content"]

        if "~" in current_res:
            current_res = current_res.replace("~", "。").replace("。。", "。")

        current_predict += current_res
        idx += 1

        if chat_item.plugins.tts and SERVER_PLUGINS_INFO.tts_server_enabled:
            # 切句子
            sentence = ""
            for symbol in SYMBOL_SPLITS:
                if symbol in current_res:
                    last_text_index, sentence = make_text_chunk(current_predict, last_text_index)
                    if len(sentence) <= 3:
                        # 文字太短的情况，不做生成
                        sentence = ""
                    break

            if sentence != "":
                sentence_id += 1
                logger.info(f"get sentence: {sentence}")
                tts_request_dict = {
                    "user_id": chat_item.user_id,
                    "request_id": chat_item.request_id,
                    "sentence": sentence,
                    "chunk_id": sentence_id,
                    # "wav_save_name": chat_item.request_id + f"{str(sentence_id).zfill(8)}.wav",
                }

                TTS_TEXT_QUENE.put(tts_request_dict)
                await asyncio.sleep(0.01)

        yield json.dumps(
            {
                "event": "message",
                "retry": 100,
                "id": idx,
                "data": current_predict,
                "step": "llm",
                "end_flag": False,
            },
            ensure_ascii=False,
        )
        await asyncio.sleep(0.01)  # 加个延时避免无法发出 event stream

    if chat_item.plugins.digital_human and SERVER_PLUGINS_INFO.digital_human_server_enabled:

        wav_list = [
            Path(WEB_CONFIGS.TTS_WAV_GEN_PATH, chat_item.request_id + f"-{str(i).zfill(8)}.wav")
            for i in range(1, sentence_id + 1)
        ]
        while True:
            # 等待 TTS 生成完成
            not_exist_count = 0
            for tts_wav in wav_list:
                if not tts_wav.exists():
                    not_exist_count += 1

            logger.info(f"still need to wait for {not_exist_count}/{sentence_id} wav generating...")
            if not_exist_count == 0:
                break

            yield json.dumps(
                {
                    "event": "message",
                    "retry": 100,
                    "id": idx,
                    "data": current_predict,
                    "step": "tts",
                    "end_flag": False,
                },
                ensure_ascii=False,
            )
            await asyncio.sleep(1)  # 加个延时避免无法发出 event stream

        # 合并 tts
        tts_save_path = Path(WEB_CONFIGS.TTS_WAV_GEN_PATH, chat_item.request_id + ".wav")
        all_tts_data = []

        for wav_file in tqdm(wav_list):
            logger.info(f"Reading wav file {wav_file}...")
            with wave.open(str(wav_file), "rb") as wf:
                all_tts_data.append([wf.getparams(), wf.readframes(wf.getnframes())])

        logger.info(f"Merging wav file to {tts_save_path}...")
        tts_params = max([tts_data[0] for tts_data in all_tts_data])
        with wave.open(str(tts_save_path), "wb") as wf:
            wf.setparams(tts_params)  # 使用第一个音频参数

            for wf_data in all_tts_data:
                wf.writeframes(wf_data[1])
        logger.info(f"Merged wav file to {tts_save_path} !")

        # 生成数字人视频
        tts_request_dict = {
            "user_id": chat_item.user_id,
            "request_id": chat_item.request_id,
            "chunk_id": 0,
            "tts_path": str(tts_save_path),
        }

        logger.info(f"Generating digital human...")
        DIGITAL_HUMAN_QUENE.put(tts_request_dict)
        while True:
            if (
                Path(WEB_CONFIGS.DIGITAL_HUMAN_VIDEO_OUTPUT_PATH)
                .joinpath(Path(tts_save_path).stem + ".mp4")
                .with_suffix(".txt")
                .exists()
            ):
                break
            yield json.dumps(
                {
                    "event": "message",
                    "retry": 100,
                    "id": idx,
                    "data": current_predict,
                    "step": "dg",
                    "end_flag": False,
                },
                ensure_ascii=False,
            )
            await asyncio.sleep(1)  # 加个延时避免无法发出 event stream

        # 删除过程文件
        for wav_file in wav_list:
            wav_file.unlink()

    yield json.dumps(
        {
            "event": "message",
            "retry": 100,
            "id": idx,
            "data": current_predict,
            "step": "all",
            "end_flag": True,
        },
        ensure_ascii=False,
    )


async def get_all_streamer_info():
    # 加载对话配置文件
    with open(WEB_CONFIGS.STREAMER_CONFIG_PATH, "r", encoding="utf-8") as f:
        streamer_info = yaml.safe_load(f)

    for i in streamer_info:
        # 将性格 list 变为字符串
        i["character"] = "、".join(i["character"])

    return streamer_info


async def get_streamer_info_by_id(id: int):
    streamer_list = await get_all_streamer_info()

    pick_info = []
    for i in streamer_list:
        if i["id"] == id:
            pick_info = [i]
            break

    return pick_info


async def get_streaming_room_info(id=-1):
    # 加载对话配置文件
    with open(WEB_CONFIGS.STREAMING_ROOM_CONFIG_PATH, "r", encoding="utf-8") as f:
        streaming_room_info = yaml.safe_load(f)

    if id <= 0:
        # 全部返回
        return streaming_room_info

    # 选择特定的直播间
    for room_info in streaming_room_info:
        if room_info["room_id"] == id:
            return room_info

    return None


async def update_streaming_room_info(id, new_info):
    # 加载直播间文件
    with open(WEB_CONFIGS.STREAMING_ROOM_CONFIG_PATH, "r", encoding="utf-8") as f:
        streaming_room_info = yaml.safe_load(f)

    # 选择特定的直播间
    for idx, room_info in enumerate(streaming_room_info):
        if room_info["room_id"] == id:
            streaming_room_info[idx] = new_info

    # 保存
    with open(WEB_CONFIGS.STREAMING_ROOM_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(streaming_room_info, f, allow_unicode=True)


async def get_llm_product_prompt_base_info():
    # 加载对话配置文件
    with open(WEB_CONFIGS.CONVERSATION_CFG_YAML_PATH, "r", encoding="utf-8") as f:
        dataset_yaml = yaml.safe_load(f)

    return dataset_yaml


async def get_conversation_list(conversion_id: str):

    if conversion_id == "":
        return []

    # 获取对应的 conversion ID 信息
    with open(WEB_CONFIGS.CONVERSATION_MESSAGE_STORE_CONFIG_PATH, "r", encoding="utf-8") as f:
        conversation_all = yaml.safe_load(f)

    if conversation_all is None:
        return []

    conversation_list = conversation_all[conversion_id]

    # 根据 message index 排序
    conversation_list_sorted = sorted(conversation_list, key=lambda item: item["messageIndex"])

    return conversation_list_sorted


async def update_conversation_message_info(id, new_info):

    with open(WEB_CONFIGS.CONVERSATION_MESSAGE_STORE_CONFIG_PATH, "r", encoding="utf-8") as f:
        streaming_room_info = yaml.safe_load(f)

    if streaming_room_info is None:
        # 初始化文件内容
        streaming_room_info = dict()

    streaming_room_info[id] = new_info

    with open(WEB_CONFIGS.CONVERSATION_MESSAGE_STORE_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(streaming_room_info, f, allow_unicode=True)


async def get_user_info(id: str):
    user_info = {
        "userName": "No1-user",
        "avater": "https://cube.elemecdn.com/0/88/03b0d39583f48206768a7534e55bcpng.png",
    }

    return user_info


@dataclass
class ResultCode:
    SUCCESS: int = 0000  # 成功
    FAIL: int = 1000  # 失败


def make_return_data(success_flag: bool, code: ResultCode, message: str, data: dict):
    return {
        "success": success_flag,
        "code": code,
        "message": message,
        "data": data,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    :   user_db.py
@Time    :   2024/08/31
@Project :   https://github.com/PeterH0323/Streamer-Sales
@Author  :   HinGwenWong
@Version :   1.0
@Desc    :   用户信息数据库操作
"""

from ipaddress import IPv4Address

from loguru import logger
from sqlmodel import Session, select

from ..models.user_model import UserBaseInfo, UserInfo
from .init_db import DB_ENGINE


def create_default_user():
    """创建默认用户"""
    admin_user = UserInfo(
        username="hingwen.wong",
        ip_address=IPv4Address("127.0.0.1"),
        email="peterhuang0323@qq.com",
        hashed_password="$2b$12$zXXveodjipHZMoSxJz5ODul7Z9YeRJd0GeSBjpwHdqEtBbAFvEdre",  # 123456 -> 用 get_password_hash 加密后的字符串
        avatar="https://cube.elemecdn.com/0/88/03b0d39583f48206768a7534e55bcpng.png",
    )

    with Session(DB_ENGINE) as session:
        session.add(admin_user)
        session.commit()


def init_user() -> bool:
    """判断是否需要创建默认用户

    Returns:
        bool: 是否执行创建默认用户
    """
    with Session(DB_ENGINE) as session:
        results = session.exec(select(UserInfo).where(UserInfo.user_id == 1)).first()

    if results is None:
        # 如果数据库为空，创建初始用户
        create_default_user()
        logger.info("created default user info")
        return True

    return False


def get_db_user_info(id: int = -1, username: str = "", all_info: bool = False) -> UserBaseInfo | UserInfo | None:
    """查询数据库获取用户信息

    Args:
        id (int): 用户 ID
        username (str): 用户名
        all_info (bool): 是否返回含有密码串的敏感信息

    Returns:
        UserInfo | None: 用户信息，没有查到返回 None
    """

    if username == "":
        # 使用 ID 的方式进行查询
        query = select(UserInfo).where(UserInfo.user_id == id)
    else:
        query = select(UserInfo).where(UserInfo.username == username)

    # 查询数据库
    with Session(DB_ENGINE) as session:
        results = session.exec(query).first()

    if results is not None and all_info is False:
        # 返回不含用户敏感信息的基本信息
        results = UserBaseInfo(**results.model_dump())

    return results

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
扫码登录，把 cookie 和 refresh_token 写进 config/config.json。

    python login.py              扫码登录，新增或更新对应账户
    python login.py --check      只检查现有账户的 cookie 状态，不改动配置
    python login.py --qr full    二维码显示错乱时换渲染方式（auto/half/full/text）

扫一次码即可：之后 judgement.py 每次运行会自动续期，不用再来手动换 cookie。
'''

import asyncio
import logging
import sys
from collections import OrderedDict

import aiohttp

import bili_cookie

DEFAULT_HEADER = OrderedDict([
    ('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36'),
    ('Referer', 'https://www.bilibili.com/'),
    ('Connection', 'keep-alive'),
])


def _skeleton():
    '''配置文件不存在时的最小骨架'''
    return OrderedDict([
        ('http_header', DEFAULT_HEADER),
        ('default_vote', OrderedDict([('mode', 1), ('vote', [0, 1]), ('once', True)])),
        ('users', []),
        ('push', OrderedDict([('enable', False), ('msgtpye', [])])),
    ])


async def do_login(qr_mode: str = 'auto'):
    try:
        configData = bili_cookie.load_config()
    except RuntimeError:
        logging.info(f'未找到配置文件，将新建：{bili_cookie.CONFIG_PATH}')
        configData = _skeleton()

    headers = configData.get('http_header') or DEFAULT_HEADER
    result = await bili_cookie.qr_login(headers, qr_mode=qr_mode)
    if not result:
        return 1

    uid = result['cookieDatas'].get('DedeUserID', '')
    if not result['refresh_token']:
        logging.warning('【登录】未拿到 refresh_token，自动续期将不可用')

    users = configData.setdefault('users', [])
    for user in users:
        if user.get('cookieDatas', {}).get('DedeUserID') == uid:
            user.update(result)          # 保留该账户下你自己加的其他字段
            logging.info(f'【登录】已更新账户 {uid}')
            break
    else:
        users.append(result)
        logging.info(f'【登录】已新增账户 {uid}')

    bili_cookie.save_config(configData)
    logging.info(f'【登录】配置已写入 {bili_cookie.CONFIG_PATH}')
    return 0


async def do_check():
    configData = bili_cookie.load_config()
    users = configData.get('users') or []
    if not users:
        logging.info('配置里没有账户')
        return 0

    connector = aiohttp.TCPConnector(limit=10, force_close=True)
    async with aiohttp.ClientSession(
            headers=configData.get('http_header') or DEFAULT_HEADER,
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=30), trust_env=True) as session:
        for user in users:
            cookie_datas = user.get('cookieDatas') or {}
            uid = cookie_datas.get('DedeUserID', '未知')
            session.cookie_jar.clear()
            session.cookie_jar.update_cookies(cookie_datas)

            async with session.get('https://api.bilibili.com/x/web-interface/nav') as r:
                nav = await r.json()
            if nav.get('code') != 0:
                logging.error(f'{uid}：cookie 已失效（nav {nav.get("code")}）'
                              f'，需要重新扫码登录')
                continue

            uname = nav['data']['uname']
            has_token = bool((user.get('refresh_token') or '').strip())
            try:
                should, _ts = await bili_cookie.needs_refresh(
                    session, cookie_datas.get('bili_jct', ''))
                state = '需要刷新' if should else '无需刷新'
            except Exception as er:
                state = f'查询失败（{er}）'

            missing = [k for k in ('buvid3', 'buvid4', 'b_nut', 'bili_ticket')
                       if not cookie_datas.get(k)]
            logging.info(
                f'{uid}（{uname}）：cookie 有效 | 服务端状态：{state} | '
                f'refresh_token：{"已配置" if has_token else "缺失，无法自动续期"} | '
                f'设备 cookie 缺：{"、".join(missing) if missing else "无"}')
    return 0


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO, format='[%(asctime)s] [%(levelname)s]: %(message)s')
    qr_mode = 'auto'
    if '--qr' in sys.argv:
        idx = sys.argv.index('--qr')
        if idx + 1 < len(sys.argv):
            qr_mode = sys.argv[idx + 1]

    try:
        if '--check' in sys.argv:
            sys.exit(asyncio.run(do_check()))
        sys.exit(asyncio.run(do_login(qr_mode)))
    except KeyboardInterrupt:
        print()
        logging.info('已取消')
        sys.exit(130)
    except Exception as er:
        logging.error(f'执行失败：{er}')
        sys.exit(1)

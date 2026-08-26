#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
B 站 Cookie 维护模块：扫码登录 + 官方 Cookie 续期 + 设备 Cookie 自动补齐

对外接口：
    await maintain_cookies(configData)   judgement.py 调用，就地更新 configData 并落盘
    await qr_login(headers)              扫码登录，返回 {'cookieDatas': {...}, 'refresh_token': str}
    load_config() / save_config(cfg)     配置读写（原子落盘 + 自动备份）

设计要点
  1. 续期严格按官方顺序：cookie/info -> CorrespondPath -> refresh_csrf -> cookie/refresh
     -> **先落盘** -> confirm/refresh
     「先落盘再 confirm」是刻意的：confirm 会作废旧 cookie。若顺序反了而中途崩溃，
     就会出现「新 cookie 没存下、旧 cookie 已失效」= 账号掉线，只能重新扫码。
  2. CorrespondPath 需要的 RSA-OAEP(SHA-256) 用标准库实现（hashlib + 内置 pow），
     不引入 pycryptodome。
  3. refresh_token 不是 cookie，存在 users[i].refresh_token；
     设备类 cookie（buvid3/buvid4/b_nut/bili_ticket）存在 users[i].cookieDatas 里，
     这样 judgement.py 原有的 update_cookies(cookieData) 会自动带上，无需改动它。
'''

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path

import aiohttp

CONFIG_PATH = Path(__file__).resolve().parent / 'config' / 'config.json'

# CorrespondPath 用的固定公钥（来自官方 Web 端 wasm 的社区逆向结果）
_BILI_PUBKEY_PEM = '''-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----'''

_TICKET_HMAC_KEY = b'XgwSnGZ1p'       # bili_ticket 签名密钥
_TICKET_TTL = 259260                   # bili_ticket 有效期，3 天


# ============================================================ RSA-OAEP(SHA-256)

def _parse_pem_pubkey(pem: str):
    '''从 PEM 解出 RSA 公钥 (n, e)，纯标准库 DER 解析'''
    der = base64.b64decode(''.join(pem.strip().splitlines()[1:-1]))

    def read_tlv(buf, i):
        i += 1                                    # 跳过 tag
        length = buf[i]
        i += 1
        if length & 0x80:                         # 长格式长度
            nbytes = length & 0x7F
            length = int.from_bytes(buf[i:i + nbytes], 'big')
            i += nbytes
        return buf[i:i + length], i + length

    spki, _ = read_tlv(der, 0)                    # SubjectPublicKeyInfo
    _algid, j = read_tlv(spki, 0)                 # AlgorithmIdentifier
    bitstr, _ = read_tlv(spki, j)                 # BIT STRING
    rsapub, _ = read_tlv(bitstr[1:], 0)           # 去掉 unused-bits 字节
    n_bytes, k = read_tlv(rsapub, 0)
    e_bytes, _ = read_tlv(rsapub, k)
    return int.from_bytes(n_bytes, 'big'), int.from_bytes(e_bytes, 'big')


_PUB_N, _PUB_E = _parse_pem_pubkey(_BILI_PUBKEY_PEM)


def _mgf1_sha256(seed: bytes, length: int) -> bytes:
    out = b''
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(seed + counter.to_bytes(4, 'big')).digest()
        counter += 1
    return out[:length]


def _rsa_oaep_encrypt(msg: bytes) -> bytes:
    '''RSA-OAEP(SHA-256, MGF1-SHA256, 空 label) 加密，等价于 pycryptodome 的 PKCS1_OAEP'''
    k = (_PUB_N.bit_length() + 7) // 8            # 模长字节数，1024 位 -> 128
    h_len = 32
    if len(msg) > k - 2 * h_len - 2:
        raise ValueError('消息过长')
    l_hash = hashlib.sha256(b'').digest()
    ps = b'\x00' * (k - len(msg) - 2 * h_len - 2)
    db = l_hash + ps + b'\x01' + msg
    seed = os.urandom(h_len)
    db_mask = _mgf1_sha256(seed, k - h_len - 1)
    masked_db = bytes(a ^ b for a, b in zip(db, db_mask))
    seed_mask = _mgf1_sha256(masked_db, h_len)
    masked_seed = bytes(a ^ b for a, b in zip(seed, seed_mask))
    em = b'\x00' + masked_seed + masked_db
    return pow(int.from_bytes(em, 'big'), _PUB_E, _PUB_N).to_bytes(k, 'big')


def get_correspond_path(timestamp_ms: int) -> str:
    '''由毫秒时间戳生成 CorrespondPath'''
    return _rsa_oaep_encrypt(f'refresh_{timestamp_ms}'.encode()).hex()


# ============================================================ 配置读写

def load_config():
    '''加载配置文件'''
    if not CONFIG_PATH.exists():
        raise RuntimeError(f'未找到配置文件：{CONFIG_PATH}')
    with open(CONFIG_PATH, 'r', encoding='utf-8') as fp:
        return json.loads(fp.read(), object_pairs_hook=OrderedDict)


def save_config(configData) -> None:
    '''
    原子落盘：先写同目录临时文件并 fsync，再 os.replace 覆盖。
    覆盖前把原文件另存为 config.json.bak，便于凭证写坏时回滚。
    '''
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            backup = CONFIG_PATH.with_suffix('.json.bak')
            backup.write_bytes(CONFIG_PATH.read_bytes())
        except OSError as er:
            logging.warning(f'【Cookie】备份旧配置失败（继续写入）：{er}')

    tmp = CONFIG_PATH.with_suffix('.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as fp:
        json.dump(configData, fp, ensure_ascii=False, indent=4)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, CONFIG_PATH)


# ============================================================ 工具

def _cookies_from_response(response) -> dict:
    '''从响应的 Set-Cookie 头里取出 cookie 键值'''
    out = {}
    for raw in response.headers.getall('Set-Cookie', []):
        first = raw.split(';', 1)[0]
        if '=' in first:
            key, value = first.split('=', 1)
            out[key.strip()] = value.strip()
    return out


def _user_label(user: dict) -> str:
    return user.get('cookieDatas', {}).get('DedeUserID') or '未知账户'


# ============================================================ 设备 Cookie

async def fill_device_cookies(session, user: dict) -> bool:
    '''
    补齐设备类 cookie，降低风控（412）概率。已存在的不重复获取。
    返回是否有改动。

    注意 bili_ticket 的到期时间记在 user 级别而不是 cookieDatas 里——
    cookieDatas 会被整体塞进 cookie jar 发给 B 站，掺进非 cookie 字段不合适。
    '''
    cookie_datas = user['cookieDatas']
    changed = False

    # buvid3 / buvid4：设备指纹，取一次就固定下来
    if not cookie_datas.get('buvid3') or not cookie_datas.get('buvid4'):
        try:
            url = 'https://api.bilibili.com/x/frontend/finger/spi'
            async with session.get(url) as r:
                ret = await r.json()
            if ret.get('code') == 0:
                cookie_datas['buvid3'] = ret['data']['b_3']
                cookie_datas['buvid4'] = ret['data']['b_4']
                cookie_datas.setdefault('b_nut', str(int(time.time())))
                changed = True
                logging.info('【Cookie】已获取 buvid3 / buvid4')
            else:
                logging.warning(f'【Cookie】buvid 获取失败：{ret.get("message")}')
        except Exception as er:
            logging.warning(f'【Cookie】buvid 获取异常：{er}')

    if not cookie_datas.get('b_nut'):
        cookie_datas['b_nut'] = str(int(time.time()))
        changed = True

    # bili_ticket：3 天有效，过期前 6 小时就换
    expires = int(user.get('bili_ticket_expires') or 0)
    if not cookie_datas.get('bili_ticket') or expires - time.time() < 6 * 3600:
        try:
            ts = int(time.time())
            hexsign = hmac.new(_TICKET_HMAC_KEY,
                               f'ts{ts}'.encode(), hashlib.sha256).hexdigest()
            url = ('https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket'
                   '/GenWebTicket')
            params = {
                'key_id': 'ec02',
                'hexsign': hexsign,
                'context[ts]': str(ts),
                'csrf': cookie_datas.get('bili_jct', ''),
            }
            async with session.post(url, params=params) as r:
                ret = await r.json()
            if ret.get('code') == 0:
                cookie_datas['bili_ticket'] = ret['data']['ticket']
                created = int(ret['data'].get('created_at') or ts)
                user['bili_ticket_expires'] = created + _TICKET_TTL
                changed = True
                logging.info('【Cookie】已刷新 bili_ticket')
            else:
                logging.warning(f'【Cookie】bili_ticket 获取失败：{ret.get("message")}')
        except Exception as er:
            logging.warning(f'【Cookie】bili_ticket 获取异常：{er}')

    return changed


# ============================================================ Cookie 续期

async def needs_refresh(session, bili_jct: str):
    '''
    查询是否需要刷新 cookie。
    返回 (需要刷新: bool, 服务端毫秒时间戳: int)；账号已掉线时抛 RuntimeError。
    '''
    url = 'https://passport.bilibili.com/x/passport-login/web/cookie/info'
    async with session.get(url, params={'csrf': bili_jct}) as r:
        ret = await r.json()
    if ret.get('code') != 0:
        raise RuntimeError(f'cookie/info 返回 {ret.get("code")}：{ret.get("message")}')
    return bool(ret['data']['refresh']), int(ret['data']['timestamp'])


async def _get_refresh_csrf(session, timestamp_ms: int) -> str:
    '''访问 correspond 页面，从 HTML 里取出实时刷新口令 refresh_csrf'''
    path = get_correspond_path(timestamp_ms)
    url = f'https://www.bilibili.com/correspond/1/{path}'
    async with session.get(url) as r:
        if r.status == 404:
            raise RuntimeError('correspond 页面 404，CorrespondPath 过期或时间戳不对')
        html = await r.text()
    match = re.search(r'<div\s+id=["\']?1-name["\']?\s*>\s*([0-9a-fA-F]+)\s*</div>', html)
    if not match:
        raise RuntimeError('未能从 correspond 页面解析出 refresh_csrf')
    return match.group(1)


async def refresh_cookie(session, user: dict, configData) -> bool:
    '''
    对单个账户执行完整的 cookie 续期流程。返回是否实际刷新过。
    成功后 user 会被就地更新，且在 confirm 之前已经落盘。
    '''
    cookie_datas = user['cookieDatas']
    label = _user_label(user)
    old_refresh_token = (user.get('refresh_token') or '').strip()

    should, server_ts = await needs_refresh(session, cookie_datas.get('bili_jct', ''))
    if not should:
        logging.info(f'{label}：【Cookie】服务端反馈无需刷新')
        return False

    logging.info(f'{label}：【Cookie】服务端提示需要刷新，开始续期')
    if not old_refresh_token:
        raise RuntimeError(
            '缺少 refresh_token，无法续期。请先运行 `python login.py` 扫码登录，'
            '或手动把浏览器 localStorage 的 ac_time_value 填入配置的 refresh_token')

    refresh_csrf = await _get_refresh_csrf(session, server_ts)

    url = 'https://passport.bilibili.com/x/passport-login/web/cookie/refresh'
    post_data = {
        'csrf': cookie_datas.get('bili_jct', ''),
        'refresh_csrf': refresh_csrf,
        'source': 'main_web',
        'refresh_token': old_refresh_token,
    }
    async with session.post(url, data=post_data) as r:
        new_cookies = _cookies_from_response(r)
        ret = await r.json()

    code = ret.get('code')
    if code != 0:
        if code == 86095:
            raise RuntimeError(
                'refresh_token 与当前 cookie 不匹配（86095）。'
                '通常是这个号在浏览器里登录过、cookie 已被浏览器换掉，需要重新扫码登录')
        raise RuntimeError(f'cookie/refresh 返回 {code}：{ret.get("message")}')

    new_refresh_token = ret['data']['refresh_token']
    if not new_cookies.get('SESSDATA') or not new_cookies.get('bili_jct'):
        raise RuntimeError('刷新接口未下发新的 SESSDATA / bili_jct，中止以免写坏配置')

    # 就地更新：新 cookie + 新 refresh_token
    for key in ('SESSDATA', 'bili_jct', 'DedeUserID', 'DedeUserID__ckMd5', 'sid'):
        if key in new_cookies:
            cookie_datas[key] = new_cookies[key]
    user['refresh_token'] = new_refresh_token

    # 关键：先落盘，再 confirm。confirm 会作废旧 cookie，顺序反了崩一次就掉号。
    save_config(configData)
    logging.info(f'{label}：【Cookie】新 cookie 已落盘')

    # 重置 cookie jar 再灌入新 cookie。
    # 原因：手工 update_cookies 的条目域名为空，与刷新响应 Set-Cookie 带域名的条目
    # 会分别存在两个桶里，同名 cookie 谁生效取决于遍历顺序——凭证不能靠这种巧合。
    session.cookie_jar.clear()
    session.cookie_jar.update_cookies(cookie_datas)

    url = 'https://passport.bilibili.com/x/passport-login/web/confirm/refresh'
    async with session.post(url, data={
        'csrf': cookie_datas['bili_jct'],          # 必须用新的 bili_jct
        'refresh_token': old_refresh_token,        # 必须用旧的 refresh_token
    }) as r:
        ret = await r.json()
    if ret.get('code') == 0:
        logging.info(f'{label}：【Cookie】续期完成，旧 cookie 已作废')
    else:
        # 落盘已完成，新 cookie 可用；旧 token 未作废只是安全瑕疵，下次会再试
        logging.warning(
            f'{label}：【Cookie】confirm/refresh 返回 {ret.get("code")}：'
            f'{ret.get("message")}（新 cookie 已生效，不影响使用）')
    return True


# ============================================================ 扫码登录

async def qr_login(headers: dict, timeout: int = 180, interval: int = 2,
                   qr_mode: str = 'auto'):
    '''
    扫码登录。终端打印二维码，用手机 B 站 App 扫描。
    成功返回 user 结构（含 cookieDatas / refresh_token），超时或二维码失效返回 None。
    qr_mode 见 bili_qr.render_qr，终端显示错乱时可指定 full / text。
    '''
    import bili_qr

    connector = aiohttp.TCPConnector(limit=10, force_close=True)
    async with aiohttp.ClientSession(
            headers=headers, connector=connector,
            timeout=aiohttp.ClientTimeout(total=30), trust_env=True) as session:

        url = 'https://passport.bilibili.com/x/passport-login/web/qrcode/generate'
        async with session.get(url) as r:
            ret = await r.json()
        if ret.get('code') != 0:
            logging.error(f'【登录】获取二维码失败：{ret}')
            return None

        login_url = ret['data']['url']
        qrcode_key = ret['data']['qrcode_key']

        import re
        import shutil

        rendered = bili_qr.render_qr(login_url, mode=qr_mode)
        # 终端太窄会把二维码折行，折行后根本扫不出来，提前提示比让人对着乱码猜要好
        need = len(re.sub(r'\033\[[0-9;]*m', '', rendered.splitlines()[0]))
        have = shutil.get_terminal_size((80, 24)).columns
        print()
        print(rendered)
        print()
        if have < need:
            print(f'  [!] 终端只有 {have} 列，二维码需要 {need} 列，已经折行、无法扫描！')
            print(f'    请把窗口拉宽到 {need} 列以上，或改用更窄的渲染方式：'
                  f'python login.py --qr half')
            print()
        print('  请用手机 B 站 App 扫描上方二维码')
        print('  显示错乱可换渲染方式重试：python login.py --qr full （或 --qr text）')
        print('  也可自行用本地工具把下面这行 URL 转成二维码：')
        print(f'  {login_url}')
        print('  （这串 URL 等同于登录凭证，不要贴给在线二维码网站或发给别人）')
        print()

        deadline = time.monotonic() + timeout
        poll = 'https://passport.bilibili.com/x/passport-login/web/qrcode/poll'
        last_state = None
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            async with session.get(poll, params={'qrcode_key': qrcode_key}) as r:
                new_cookies = _cookies_from_response(r)
                ret = await r.json()

            if ret.get('code') != 0:
                logging.warning(f'【登录】轮询接口异常：{ret}')
                continue

            data = ret['data']
            state = data.get('code')
            if state == 0:
                if not new_cookies.get('SESSDATA'):
                    logging.error('【登录】扫码成功但未取到 SESSDATA')
                    return None
                logging.info('【登录】扫码成功！')
                cookie_datas = OrderedDict()
                for key in ('SESSDATA', 'bili_jct', 'DedeUserID',
                            'DedeUserID__ckMd5', 'sid'):
                    if key in new_cookies:
                        cookie_datas[key] = new_cookies[key]
                user = OrderedDict([
                    ('cookieDatas', cookie_datas),
                    ('refresh_token', data.get('refresh_token', '')),
                ])
                await fill_device_cookies(session, user)
                return user
            if state == 86038:
                logging.error('【登录】二维码已失效，请重新运行')
                return None
            if state != last_state:
                # 86101 未扫码 / 86090 已扫码待确认
                logging.info(f'【登录】{data.get("message")}')
                last_state = state

        logging.error('【登录】等待超时')
        return None


# ============================================================ 对外主入口

async def maintain_cookies(configData) -> None:
    '''
    judgement.py 调用的入口：对每个账户检查续期 + 补齐设备 cookie，
    就地更新 configData 并落盘。任何账户失败都不会打断其他账户。
    '''
    users = configData.get('users') or []
    if not users:
        return

    headers = configData.get('http_header', {})
    connector = aiohttp.TCPConnector(limit=10, force_close=True)
    async with aiohttp.ClientSession(
            headers=headers, connector=connector,
            timeout=aiohttp.ClientTimeout(total=60), trust_env=True) as session:
        for user in users:
            label = _user_label(user)
            cookie_datas = user.get('cookieDatas')
            if not cookie_datas:
                continue

            # 每个账户用自己的 cookie，避免串号
            session.cookie_jar.clear()
            session.cookie_jar.update_cookies(cookie_datas)

            try:
                await refresh_cookie(session, user, configData)
            except Exception as er:
                logging.warning(f'{label}：【Cookie】续期未完成：{er}')

            try:
                # 刷新后 cookie 可能已变，重新灌一遍再取设备 cookie
                session.cookie_jar.clear()
                session.cookie_jar.update_cookies(user['cookieDatas'])
                if await fill_device_cookies(session, user):
                    save_config(configData)
            except Exception as er:
                logging.warning(f'{label}：【Cookie】设备 cookie 补齐失败：{er}')

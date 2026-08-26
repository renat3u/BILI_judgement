#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
极简 QR Code 生成器 + 终端渲染，纯 Python 零依赖。

只实现够用的子集：字节模式、纠错等级 L、版本 1~10（最长 274 字节），
足够放 B 站扫码登录的 URL（约 120 字节）。

对外只有两个函数：
    make_matrix(data)  -> list[list[bool]]   True 为深色模块
    render_qr(data)    -> 终端可打印的字符串
'''

import sys

# 版本 -> (每块纠错码字数, [(块数, 每块数据码字数), ...])   纠错等级 L
_RS_BLOCKS_L = {
    1: (7, [(1, 19)]),
    2: (10, [(1, 34)]),
    3: (15, [(1, 55)]),
    4: (20, [(1, 80)]),
    5: (26, [(1, 108)]),
    6: (18, [(2, 68)]),
    7: (20, [(2, 78)]),
    8: (24, [(2, 97)]),
    9: (30, [(2, 116)]),
    10: (18, [(2, 68), (2, 69)]),
}

# 版本 -> 校正图形中心坐标
_ALIGN_CENTERS = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

_MAX_VERSION = 10


# ---------------------------------------------------------------- GF(256)

def _build_gf():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:          # 本原多项式 x^8+x^4+x^3+x^2+1 = 0x11D
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _build_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(degree: int) -> list:
    '''生成多项式 (x-a^0)(x-a^1)...'''
    poly = [1]
    for i in range(degree):
        poly = _poly_mul(poly, [1, _GF_EXP[i]])
    return poly


def _poly_mul(a: list, b: list) -> list:
    out = [0] * (len(a) + len(b) - 1)
    for i, av in enumerate(a):
        for j, bv in enumerate(b):
            out[i + j] ^= _gf_mul(av, bv)
    return out


def _rs_ecc(data: list, ec_len: int) -> list:
    '''计算 Reed-Solomon 纠错码字'''
    gen = _rs_generator(ec_len)
    rem = list(data) + [0] * ec_len
    for i in range(len(data)):
        coef = rem[i]
        if coef:
            for j in range(1, len(gen)):
                rem[i + j] ^= _gf_mul(gen[j], coef)
    return rem[len(data):]


# ---------------------------------------------------------------- 数据编码

def _pick_version(nbytes: int) -> int:
    for v in range(1, _MAX_VERSION + 1):
        ec_len, groups = _RS_BLOCKS_L[v]
        capacity = sum(cnt * dpb for cnt, dpb in groups)
        # 4 位模式指示符 + 字符计数指示符(版本1-9为8位,10-40为16位)
        header_bits = 4 + (8 if v < 10 else 16)
        if nbytes + (header_bits + 7) // 8 <= capacity:
            return v
    raise ValueError(f'数据过长（{nbytes} 字节），本简化实现最多支持版本 {_MAX_VERSION}')


def _encode_data(data: bytes, version: int) -> list:
    '''编码为码字序列（含纠错、交织）'''
    ec_len, groups = _RS_BLOCKS_L[version]
    total_data = sum(cnt * dpb for cnt, dpb in groups)

    bits = []

    def put(value: int, length: int):
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)                                  # 字节模式
    put(len(data), 8 if version < 10 else 16)       # 字符计数
    for byte in data:
        put(byte, 8)

    # 结束符，最多 4 个 0
    put(0, min(4, total_data * 8 - len(bits)))
    # 补齐到字节边界
    while len(bits) % 8:
        bits.append(0)
    # 填充字节 0xEC / 0x11 交替
    pad = [0xEC, 0x11]
    i = 0
    while len(bits) // 8 < total_data:
        put(pad[i % 2], 8)
        i += 1

    codewords = [int(''.join(map(str, bits[i:i + 8])), 2)
                 for i in range(0, len(bits), 8)]

    # 分块
    blocks, ecc_blocks, pos = [], [], 0
    for cnt, dpb in groups:
        for _ in range(cnt):
            blk = codewords[pos:pos + dpb]
            pos += dpb
            blocks.append(blk)
            ecc_blocks.append(_rs_ecc(blk, ec_len))

    # 交织数据码字
    out = []
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    # 交织纠错码字
    for i in range(ec_len):
        for b in ecc_blocks:
            out.append(b[i])
    return out


# ---------------------------------------------------------------- 矩阵构建

def _place_function_patterns(m: list, reserved: list, version: int):
    size = len(m)

    def finder(r0, c0):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                # 7x7 回字 + 1 模块分隔符
                inside = 0 <= dr <= 6 and 0 <= dc <= 6
                dark = inside and (
                    dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))
                m[r][c] = dark
                reserved[r][c] = True

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    # 定位图形
    for i in range(8, size - 8):
        m[6][i] = m[i][6] = (i % 2 == 0)
        reserved[6][i] = reserved[i][6] = True

    # 校正图形
    centers = _ALIGN_CENTERS[version]
    for r in centers:
        for c in centers:
            # 跳过与探测图形重叠的三个角
            if (r, c) in ((6, 6), (6, centers[-1]), (centers[-1], 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = (
                        max(abs(dr), abs(dc)) != 1)
                    reserved[r + dr][c + dc] = True

    # 格式信息区预留
    for i in range(9):
        reserved[8][i] = True
        reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True

    # 固定深色模块
    m[size - 8][8] = True
    reserved[size - 8][8] = True

    # 版本信息区预留（版本 >= 7）
    if version >= 7:
        for i in range(6):
            for j in range(3):
                reserved[size - 11 + j][i] = True
                reserved[i][size - 11 + j] = True


def _place_data(m: list, reserved: list, codewords: list):
    size = len(m)
    bits = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)

    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:            # 跳过竖向定位图形所在列
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if reserved[row][c]:
                    continue
                m[row][c] = bool(bits[idx]) if idx < len(bits) else False
                idx += 1
        upward = not upward
        col -= 2


def _mask_fn(mask: int, r: int, c: int) -> bool:
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0


def _bch_format(data: int) -> int:
    '''格式信息 BCH(15,5)'''
    d = data << 10
    while d.bit_length() - 1 >= 10:
        d ^= 0x537 << (d.bit_length() - 11)
    return ((data << 10) | d) ^ 0x5412


def _bch_version(version: int) -> int:
    '''版本信息 BCH(18,6)'''
    d = version << 12
    while d.bit_length() - 1 >= 12:
        d ^= 0x1F25 << (d.bit_length() - 13)
    return (version << 12) | d


def _place_format_info(m: list, version: int, mask: int):
    size = len(m)
    fmt = _bch_format((0b01 << 3) | mask)      # 纠错等级 L = 0b01

    # 竖向副本：第 8 列
    for i in range(15):
        bit = bool((fmt >> i) & 1)
        if i < 6:
            m[i][8] = bit
        elif i < 8:
            m[i + 1][8] = bit
        else:
            m[size - 15 + i][8] = bit

    # 横向副本：第 8 行
    for i in range(15):
        bit = bool((fmt >> i) & 1)
        if i < 8:
            m[8][size - 1 - i] = bit
        elif i == 8:
            m[8][7] = bit
        else:
            m[8][15 - i - 1] = bit

    if version >= 7:
        ver = _bch_version(version)
        for i in range(18):
            bit = bool((ver >> i) & 1)
            m[size - 11 + i % 3][i // 3] = bit
            m[i // 3][size - 11 + i % 3] = bit


def _penalty(m: list) -> int:
    size = len(m)
    score = 0

    # 规则1：行/列连续同色 >= 5
    for line in list(m) + [list(col) for col in zip(*m)]:
        run, prev = 1, line[0]
        for v in line[1:]:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, prev = 1, v
        if run >= 5:
            score += 3 + (run - 5)

    # 规则2：2x2 同色块
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3

    # 规则3：1:1:3:1:1 模式
    pat1 = [True, False, True, True, True, False, True,
            False, False, False, False]
    pat2 = pat1[::-1]
    for line in list(m) + [list(col) for col in zip(*m)]:
        for i in range(size - 10):
            seg = line[i:i + 11]
            if seg == pat1 or seg == pat2:
                score += 40

    # 规则4：深色比例偏离 50%，每偏离 5% 加 10 分（注意要用浮点算，不能先截断）
    dark = sum(sum(1 for v in row if v) for row in m)
    percent = dark / (size * size)
    score += 10 * int(abs(percent * 100 - 50) / 5)
    return score


def _info_cells(size: int, version: int) -> list:
    '''格式信息 / 版本信息 / 固定深色模块所占的位置'''
    cells = [(size - 8, 8)]
    for i in range(15):
        if i < 6:
            cells.append((i, 8))
        elif i < 8:
            cells.append((i + 1, 8))
        else:
            cells.append((size - 15 + i, 8))
        if i < 8:
            cells.append((8, size - 1 - i))
        elif i == 8:
            cells.append((8, 7))
        else:
            cells.append((8, 15 - i - 1))
    if version >= 7:
        for i in range(18):
            cells.append((i // 3, size - 11 + i % 3))
            cells.append((size - 11 + i % 3, i // 3))
    return cells


def make_matrix(data) -> list:
    '''生成 QR 矩阵，True 表示深色模块'''
    if isinstance(data, str):
        data = data.encode('utf-8')
    version = _pick_version(len(data))
    size = version * 4 + 17
    codewords = _encode_data(data, version)
    info_cells = _info_cells(size, version)

    best, best_score = None, None
    for mask in range(8):
        m = [[False] * size for _ in range(size)]
        reserved = [[False] * size for _ in range(size)]
        _place_function_patterns(m, reserved, version)
        _place_data(m, reserved, codewords)
        for r in range(size):
            for c in range(size):
                if not reserved[r][c] and _mask_fn(mask, r, c):
                    m[r][c] = not m[r][c]

        # 评分时格式信息区视作空白（与主流实现一致），选定掩码后再填入
        scoring = [row[:] for row in m]
        for (r, c) in info_cells:
            scoring[r][c] = False
        score = _penalty(scoring)

        if best_score is None or score < best_score:
            _place_format_info(m, version, mask)
            best, best_score = m, score
    return best


# ---------------------------------------------------------------- 终端渲染

def _stdout_supports(char: str) -> bool:
    '''终端编码能否输出该字符（最小化装的 VPS 常见 LANG=C，只能输出 ASCII）'''
    enc = getattr(sys.stdout, 'encoding', None) or 'ascii'
    try:
        char.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def render_qr(data, quiet: int = 2, mode: str = 'auto') -> str:
    '''
    渲染为可打印字符串。mode 取值：
      auto  终端编码支持 '▀' 就用 half，否则退到 full（默认）
      half  ANSI 颜色 + 半块字符，高度减半最紧凑，需要 UTF-8 终端
      full  ANSI 背景色 + 空格，纯 ASCII 字符，任何终端都能出，但高度翻倍
      text  完全不用颜色，'##' 画深色块；只适合浅色背景终端
    '''
    if mode == 'auto':
        mode = 'half' if _stdout_supports('▀') else 'full'
    if mode not in ('half', 'full', 'text'):
        raise ValueError(f'未知的渲染模式：{mode}')

    m = make_matrix(data)
    size = len(m)
    grid = [[False] * (size + quiet * 2) for _ in range(quiet)]
    for row in m:
        grid.append([False] * quiet + list(row) + [False] * quiet)
    grid += [[False] * (size + quiet * 2) for _ in range(quiet)]

    # 用背景色而不是字符形状来表达明暗，这样终端是深色还是浅色主题都不会反相
    BG = {True: '\033[40m', False: '\033[107m'}          # 深色模块 -> 黑底，浅色 -> 白底
    lines = []

    if mode == 'half':
        # '▀' 上半块：前景色画上面那行，背景色画下面那行
        FG = {True: '\033[30m', False: '\033[97m'}
        for y in range(0, len(grid), 2):
            top = grid[y]
            bot = grid[y + 1] if y + 1 < len(grid) else [False] * len(top)
            lines.append(''.join(f'{FG[top[x]]}{BG[bot[x]]}▀'
                                 for x in range(len(top))) + '\033[0m')
    elif mode == 'full':
        for row in grid:
            lines.append(''.join(f'{BG[v]}  ' for v in row) + '\033[0m')
    else:
        for row in grid:
            lines.append(''.join('##' if v else '  ' for v in row))
    return '\n'.join(lines)


def print_qr(data, **kw):
    print(render_qr(data, **kw))

# B 站风纪委员自动投票

一个跑在服务器上的 B 站风纪委员（众裁）自动投票脚本。**扫一次码即可长期运行**——内置官方 Cookie 续期流程，不用再定期手动去浏览器复制 SESSDATA。

> 二次开发自 [dd178/BILI_judgement](https://github.com/dd178/BILI_judgement)，新增了扫码登录与 Cookie 自动续期。详见[来源与致谢](#来源与致谢)。

## 特性

- **跟随多数观点投票**：拉取案件的众议观点列表，统计后跟随人数最多的一方，没有观点时按配置的默认票兜底
- **扫码登录**：终端直接打印二维码（纯 Python 实现，零依赖），手机 B 站 App 扫一下就写好配置
- **Cookie 自动续期**：每次运行前按官方 `cookie/refresh` 流程续期，并自动补齐 `buvid3` / `buvid4` / `bili_ticket` 等设备 Cookie 降低风控概率
- **资格自动续签**：检测到风纪委员资格过期时自动重新申请
- **多渠道推送**：企业微信、Telegram、Server 酱、即时达、pushplus
- **异步并发**：基于 `aiohttp`，支持多账号同时运行

## 目录结构

```
.
├── judgement.py              # 主程序：投票逻辑 + 推送
├── login.py                  # 扫码登录 / 账号状态检查
├── bili_cookie.py            # Cookie 续期、设备 Cookie 补齐、配置读写
├── bili_qr.py                # 极简二维码生成与终端渲染（纯标准库）
├── judge.py                  # 可选：每 24 小时跑一次主程序的常驻循环
├── requirements.txt
└── config/
    ├── config.json.example   # 配置模板
    ├── README.md             # 配置逐字段说明（带注释的 json5）
    └── config.json           # 你的真实配置（已被 .gitignore 忽略，不要提交）
```

## 快速开始

环境要求：Python 3.8+

```bash
git clone https://github.com/renat3u/BILI_judgement.git
cd BILI_judgement
pip install -r requirements.txt

# 1. 准备配置
cp config/config.json.example config/config.json

# 2. 扫码登录（终端会打印二维码，用手机 B 站 App 扫）
python login.py

# 3. 开跑
python judgement.py
```

`login.py` 会把 Cookie 和 `refresh_token` 自动写进 `config/config.json`，之后每次运行 `judgement.py` 都会先续期，正常情况下不需要再登录第二次。

其他命令：

```bash
python login.py --check      # 只检查现有账号的 Cookie 状态，不改动配置
python login.py --qr full    # 二维码显示错乱时换渲染方式（auto / half / full / text）
```

> 终端窗口太窄会把二维码折行导致扫不出来，脚本会提前提示所需列数。也可以用 `--qr half` 换更窄的渲染方式。

## 配置说明

### default_vote —— 投票行为

| 字段 | 说明 |
| --- | --- |
| `mode` | 遇到没有观点的案件怎么办。`1`：立刻投默认票；`2`：先记下案件 id 跳过，等案件池拉空后再回头统一投默认票——好处是把有观点的案件优先跟完 |
| `vote` | 默认票，对应案件 `vote_items` 的下标：`0` 好、`1` 普通、`2` 差、`3` 无法判断。填多个为随机投票，填一个为固定投票 |
| `once` | `true`：拉不到新案件时休眠 30 分钟后继续，直到案件审满才退出；`false`：拉不到新案件就直接退出 |

投票时会先以 `vote=0` 占位（等价于「已查看案件」），随机等待 10~20 秒后再投出正式一票，避免节奏过于机械。

### users —— 账号

推荐用 `python login.py` 自动生成。手动填写时至少需要 `SESSDATA`、`bili_jct`、`DedeUserID`；`refresh_token` 对应浏览器 `localStorage` 里的 `ac_time_value`，缺失时无法自动续期。多账号就在 `users` 数组里加多个对象。

### push —— 推送

`enable` 为总开关，`msgtpye` 控制推送哪几类消息：

| 类型 | 触发时机 |
| --- | --- |
| `CookieExpires` | Cookie 失效，需要重新扫码 |
| `UnknownError` | 连续出错超过阈值，任务中止 |
| `DailyMissions` | 本轮结束后汇报今日完成进度（x/20） |

各渠道单独有自己的 `enable`，按需填密钥即可，用不到的保持 `false`。

## 常驻运行

仓库里的 `judge.py` 是最简单的方式——一个每 24 小时调用一次主程序的循环：

```bash
nohup python3 judge.py > judge.log 2>&1 &
```

更推荐用 crontab，每天固定时间跑一次：

```cron
0 9 * * * cd /path/to/repo && /usr/bin/python3 judgement.py >> judge.log 2>&1
```

或者用 systemd timer 管理，好处是能自动重启、日志进 journald。

## 常见问题

**Cookie 提示失效 / nav 返回非 0**
重新执行 `python login.py` 扫码即可。

**续期报 86095（refresh_token 与当前 Cookie 不匹配）**
通常是这个账号在浏览器里重新登录过，Cookie 已被换掉。重新扫码登录即可。

**请求返回 412**
触发了风控。确认设备 Cookie 已补齐（`python login.py --check` 可以看到缺哪些），并适当降低运行频率。

**风纪委员资格过期**
脚本会自动尝试重新申请；若申请失败，说明账号当前不满足资格条件（等级、答题等），需要自行到 B 站处理。

## 安全提示

- `config/config.json` 里的 SESSDATA 等同于账号登录态，**泄露即等于账号被盗**。仓库已通过 `.gitignore` 忽略该文件，提交前请再次确认 `git status` 里没有它。
- 推送渠道的 token（尤其是 Telegram `bot_token`）同样属于凭证，不要写进任何会被提交的文件。
- 续期流程会在覆盖前自动备份为 `config/config.json.bak`，该备份同样含明文凭证，一并被 `.gitignore` 忽略。

## 来源与致谢

本项目是 [dd178/BILI_judgement](https://github.com/dd178/BILI_judgement) 的二次开发版本，`judgement.py` 的投票主流程来自该项目（作者 [@dd178](https://github.com/dd178)，文件头署名 `178`），版权归原作者所有。

原项目又说明其部分代码（`asyncBiliApi` 异步接口封装）抄自 [MaxSecurity/BiliExper](https://github.com/MaxSecurity/BiliExper)。

本仓库在原项目基础上新增的部分：

- `login.py`：扫码登录
- `bili_cookie.py`：官方 Cookie 续期流程、设备 Cookie 自动补齐、配置原子落盘
- `bili_qr.py`：纯标准库实现的二维码生成与终端渲染

## 免责声明

请同时阅读[原项目的特别声明](https://github.com/dd178/BILI_judgement#特别声明)，其中包含原作者对转载与发布的限制条款。

本项目仅供学习与技术交流，请勿用于商业或非法用途，也请勿用于任何违反 B 站用户协议的场景。使用本脚本产生的一切后果（包括但不限于账号被封禁）由使用者自行承担。

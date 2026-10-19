# CAN 回灌工具（Linux / SocketCAN）

把 `candump` 采集的 CAN / CAN FD 日志，按原始时间间隔回放到 Linux SocketCAN
接口。提供交互式终端菜单和命令行两种用法，适用于 Ubuntu 台架、车端 ARM 设备
以及 `vcan` 虚拟总线验证。

## 功能特点

- 纯 Python 标准库实现，不依赖 Vector、Windows 或第三方 Python 包。
- 支持带日期时间戳、epoch 时间戳、紧凑 `ID#DATA` 三种常见 candump 行格式。
- 支持经典 CAN 与 CAN FD，按日志里的 `[8]` / `[08]` 长度写法自动识别帧类型。
- 支持多文件按时间戳合并、接口映射、时间段过滤、ID 过滤和帧类型过滤。
- 支持倍速、循环、跳过前 N 帧、限制发送帧数、最小帧间隔。
- 发送前可 `--dry-run` 预览；`--info` 快速查看日志摘要；`--stats` 输出每个 ID 的统计。
- 菜单支持 Tab 补全日志路径和文件名，自动检测可用 CAN 接口，并在发送前汇总参数。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `can_replay.py` | 回灌引擎：解析日志、过滤、排序、按时间轴发送到 SocketCAN。 |
| `replay_start.sh` | 终端交互菜单：选择日志、方向、接口、节奏并确认回灌。 |
| `CLI_GUIDE.md` | 命令行使用说明：与交互菜单逐步对应的命令、参数和完整示例。 |
| `README.md` | 使用说明。 |

菜单会在脚本所在目录、`can_tools/` 以及上一级目录中查找支持 `--frame-type`
的 `can_replay.py`。把两个文件放在同一目录即可直接使用。

## 系统要求

- Linux 内核启用 SocketCAN；使用虚拟总线时启用 `vcan` 模块（未加载时执行
  `sudo modprobe vcan`）。
- Python 3.8 及以上（车端已验证 Python 3.12）。
- Bash 4 及以上（菜单使用数组和大小写转换）。
- 采集或验证时可安装 `can-utils`，其中包含 `candump`、`cansend` 等工具。
- 向真实 CAN 接口发送需要 root 权限或 `CAP_NET_RAW`；菜单会自动通过 `sudo` 提权。

## 快速开始

把文件复制到目标机器后，确认接口已启用：

```bash
chmod +x can_replay.py replay_start.sh
ip -br link show
./replay_start.sh
```

没有真实 CAN 硬件时，可以先建一个虚拟总线：

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set dev vcan0 up
ip -br link show vcan0
```

如果 `vcan0` 已存在，`ip link add` 会提示 `File exists`，直接执行 `ip link set
dev vcan0 up` 即可。

## 交互菜单

运行 `./replay_start.sh` 后依次完成 5 个步骤：

1. **选择日志目录**：可选当前目录，也可输入其他路径；路径输入支持 Tab 补全。
2. **选择日志文件**：列出编号、文件名和大小。输入文件名前缀后按 Tab 补全；
   也可以输入 `ALL`、`1,3` 或完整文件名。直接回车等于 `ALL`。
3. **选择报文过滤**：选择上行、下行或全部。方向确定后会先显示当前所选日志的
   时间段和可输入的钟点边界，再填写起止时间；随后按编号或十六进制 ID 选择报文，
   目标接口固定为 `vcan0`。
4. **设置回灌节奏**：输入倍速、循环次数（`i` 表示无限）和只灌前 N 帧
   （`0` 表示整段全部）。
5. **确认**：输入 `y` 开始，`p` 先预览前 20 帧（不发送），`n` 退出。

回灌结束后可以输入 `r` 用同样参数再跑一遍，`c` 回到过滤参数重新设置，`q` 退出。

### 菜单输入说明

| 输入项 | 可用写法 |
| --- | --- |
| 日志文件 | `ALL`、`1`、`1,3`、文件名；输入前缀后按 Tab 自动补全 |
| 报文方向 | `1` 上行 FD `[08]`；`2` 下行经典帧 `[8]`；`3` 上下行全部 |
| 时间 | `16:25`、`16:25:30`、`1625`、`162530`，也可带日期；留空表示不限 |
| 报文 ID | 留空表示全部；可填菜单编号，或 3 位 / 8 位十六进制 ID；多个用空格或逗号分隔 |

菜单不再询问目标接口，回灌统一发到 `vcan0`。请确保该接口已创建并启用；如需
改灌到其他接口，可直接使用引擎的 `--iface` / `--map` 参数。

### 菜单命令行参数

菜单脚本支持在命令行预填交互步骤；没有预填的项目仍会继续询问：

| 参数 | 对应交互步骤 | 说明 |
| --- | --- | --- |
| `-d <目录>` | 3.2 | 日志目录 |
| `-f <ALL/1,3/文件名>` | 3.3 | 日志文件 |
| `-D, --direction <值>` | 3.4 | `up`、`down`、`both`（兼容 `1/2/3` 和 `upstream/downstream`） |
| `--from <时间>` / `--to <时间>` | 3.5 | 起止时间，不写表示不限 |
| `-id, -i, --filter-id <ID>` | 3.6 | 菜单序号、区间或十六进制 ID，逗号/空格分隔 |
| `-s, --speed <倍速>` | 3.7 | 默认 `1` |
| `-l, --loop <次数\|i>` | 3.7 | 默认 `1`；`i` 表示无限 |
| `-n, --limit <N>` | 3.7 | 默认 `0`，表示整段全部 |
| `-y, --yes` | 3.8 | 跳过剩余询问并直接开始，不再要求最终确认 |

```bash
# 指定日志目录和文件，其余步骤继续交互
./replay_start.sh -d /data/canlogs -f 1,3

# 预填方向、时间和报文 ID，倍速/循环仍交互询问
./replay_start.sh -d /data/canlogs -f ALL \
    --direction up \
    --from 15:44:26 --to 15:46:00 \
    -id 181D20A0,181D21A0

# 全部预填并直接开始
./replay_start.sh -d /data/canlogs -f ALL -y \
    --direction up \
    --from 15:44:26 --to 15:46:00.999999 \
    -id 181D20A0,181D21A0 \
    --speed 2 --loop 3 --limit 100

# -- 后面仍可透传其他引擎参数，例如预览
./replay_start.sh -d /data/canlogs -f ALL -y -- \
    --dry-run --stats
```

`-d`、`-f` 以及方向、时间、ID、节奏参数都会跳过对应的交互询问；`--` 后面的
参数原样传给 `can_replay.py`。

`-id` 可以写菜单候选序号（按当前方向的候选列表）、序号区间或实际十六进制 ID，
例如 `-id 1-3,4,5,18D6F00`；`-id ALL` 表示不按 ID 过滤。

注意：`-y` 会跳过剩余询问并立即发送；未指定方向时，若日志只有 FD 帧则自动选择
上行，否则默认下行经典帧 `[8]`。未指定时间/ID/节奏时分别使用“不限、全部、
1 倍速 / 1 次 / 整段全部”。首次使用、切换台架或修改接口后，建议先加
`-- --dry-run --stats` 预览，再正式回灌。

## 命令行使用

按交互菜单步骤逐项对应的完整说明见 [CLI_GUIDE.md](CLI_GUIDE.md)。

### 先查看日志

```bash
# 日志摘要：帧数、接口、时间范围、每个 ID 的帧数
./can_replay.py --info candump.log

# 机器可读的 JSON 摘要
./can_replay.py --info --info-json candump.log

# 只解析和前 20 帧预览，不开 socket、不发帧
./can_replay.py --dry-run --stats candump.log
```

`--info` 是快速扫描，统计口径比完整解析略宽松；实际可回灌帧数以 `--dry-run`
或正式加载时的 `loaded ... frames` 输出为准。

### 回灌示例

```bash
# 原速发到日志中记录的接口（需要 root）
sudo ./can_replay.py candump.log

# 把日志里的 vcan0 映射到真实接口 can0
sudo ./can_replay.py --map vcan0=can0 candump.log

# 所有帧统一发到 can0，2 倍速，只发前 100 帧
sudo ./can_replay.py --iface can0 --speed 2 --limit 100 candump.log

# 只回灌下行经典帧，时间段 15:31:00 ~ 15:32:30
sudo ./can_replay.py --direction down \
    --from 15:31:00 --to 15:32:30 candump.log

# 只回灌指定 ID，多个 ID 用逗号分隔
sudo ./can_replay.py -id 181D20A0,181D21A0 candump.log

# 多份日志按时间戳合并后回灌
sudo ./can_replay.py logs/candump_vcan0_*.log

# CAN FD 帧带 BRS 标志（1 = BRS），原速发到日志接口
sudo ./can_replay.py --frame-type fd --fd-flags 1 candump_fd.log
```

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `logs ...` | 一个或多个 candump 日志文件；多文件按时间戳合并排序。 |
| `--iface IFACE` | 所有帧统一发到指定接口；默认使用日志中的接口名。 |
| `--map LOG=DST` | 按日志接口映射到目标接口，可重复，例如 `--map vcan2=vcan0`；优先于 `--iface`。 |
| `--speed N` | 回灌倍速，默认 `1.0`；`2` 表示两倍速。 |
| `--loop N` | 循环次数，默认 `1`；`i`、`0`、`inf` 表示无限循环。 |
| `--gap-ms N` | 相邻帧的最小间隔（毫秒），用于限制发送过快。 |
| `-id, --filter-id IDS` | 只发指定十六进制 ID，逗号或分号分隔，可重复。 |
| `--from TIME` / `--to TIME` | 只回灌指定时间段；支持 `HH:MM`、`HH:MM:SS` 或 `YYYY-MM-DD HH:MM:SS`，按本地时区解释。 |
| `--skip N` | 跳过排序后的前 N 帧。 |
| `--limit N` | 最多发送 N 帧；`0` 表示不限。 |
| `--dry-run` | 只解析并打印前 20 帧，不建立 socket、不发送。 |
| `--no-loopback` | 关闭本地回环，不把本 socket 发出的帧回灌给本地接收端。 |
| `--fd-flags HEX` | CAN FD 标志位，`0~F`；当前仅 BRS（`1`）生效，默认 `0`。 |
| `--frame-type TYPE` | `classic` 只发 `[8]` 经典帧，`fd` 只发 `[08]` FD 帧，`all` 发全部。 |
| `--direction DIR` | 台架方向别名：`up`、`down`、`both`（兼容 `upstream/downstream`）；优先于 `--frame-type`。 |
| `--stats` | 结束时打印每个 ID 的装载帧数和实际发送帧数。 |
| `--quiet` | 抑制常规进度输出；显式指定 `--stats` 时仍会打印统计。 |

完整参数和帮助：

```bash
./can_replay.py --help
./replay_start.sh -h
```

## 方向约定

本版本沿用当前工程的台架约定：

| 方向 | candump 写法 | 含义 |
| --- | --- | --- |
| 上行 | `[08]` CAN FD | 其他节点发来，ADU 接收 |
| 下行 | `[8]` 经典 CAN | ADU 发送 |
| 全部 | 两种都发 | 不做方向过滤 |

这是当前台架的业务约定，不是 CAN 协议定义。方向选择只筛选日志帧，不会自动
判定物理总线方向，也不会修改报文内容；菜单固定从 `vcan0` 发出，命令行可用
`--iface` / `--map` 指定其他物理出口。

等价写法：

```bash
./can_replay.py --direction up         ...
./can_replay.py --frame-type fd        ...
./can_replay.py --fd-only              ...

./can_replay.py --direction down       ...
./can_replay.py --frame-type classic   ...
./can_replay.py --classic-only         ...

./can_replay.py --direction both       ...
./can_replay.py --frame-type all       ...
```

## 日志格式

支持以下三种格式，可混用，也可多文件合并：

```text
(2026-09-21 15:30:00.097618)  vcan2  181D20A0  [8]  01 00 00 00 64 00 64 64
(1789975800.097618)            vcan2  181D20A0  [8]  01 00 00 00 64 00 64 64
(1789975800.097618)            vcan2  181D20A0#0100000064006464
```

长度写法决定帧类型：

- `[8]`：经典 CAN 2.0 帧，数据最多 8 字节。
- `[08]`、`[12]`、`[64]` 等两位长度：CAN FD 帧，数据最多 64 字节。
- 紧凑写法 `<id>##<flags><data>` 也表示 CAN FD，其中 `flags` 是一位十六进制数。

candump 本身不记录 FD 的 BRS / ESI 标志。需要还原 BRS 时用 `--fd-flags 1`；
ESI 目前不会还原；紧凑写法里已经带 `##<flags>` 的日志则使用日志自身的标志。
错误帧会被丢弃，RTR 帧和 canplayer 的裸行格式不在支持范围内。

多份日志会按时间戳合并并重新排序。它们必须来自同一场录制、使用同一时钟基准，
否则帧间节奏会错乱。比如同时回灌 `vcan0` 和 `vcan2` 两路日志时，可以把它们
放在同一条命令里：

```bash
sudo ./can_replay.py --map vcan0=can0 --map vcan2=can1 \
    logs/vcan0.log logs/vcan2.log
```

## vcan 闭环验证

在接入真实总线前，建议先用 `vcan` 做一遍完整验证。

### 1. 建虚拟接口

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set dev vcan0 up
```

### 2. 采集一段日志

终端 1 启动接收并写入日志：

```bash
candump -L vcan0 | tee demo.log
```

终端 2 发一帧测试报文：

```bash
cansend vcan0 123#112233
```

在终端 1 按 `Ctrl-C` 停止采集。`demo.log` 就是可用于回灌的 candump 日志。

### 3. 预览并回灌

```bash
./can_replay.py --info demo.log
./can_replay.py --dry-run --stats demo.log
sudo ./can_replay.py --iface vcan0 --speed 2 --limit 10 demo.log
```

另开一个终端执行 `candump vcan0`，应能看到回灌的报文。虚拟总线只验证软件链路，
不能代替真实 CAN 收发器、终端电阻、仲裁和 ACK 测试。

## 常见问题

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `Operation not permitted` | 没有 root 权限或 `CAP_NET_RAW` | 用 `sudo` 运行；菜单会自动提权。 |
| `cannot open can0: No such device` | 接口不存在或名字写错 | 用 `ip -br link show` 检查；先建 `vcan` 或确认真实接口已加载。 |
| `Network is down` | 接口没有启用 | `sudo ip link set dev can0 up`。 |
| FD 帧发送报 `Invalid argument` | 接口未启用 CAN FD 或未配置数据域波特率 | 在真实接口上配置 `dbitrate` / `fd on`；先用 `vcan` 验证。 |
| `no frames to replay` | 方向、时间段或 ID 过滤后为空 | 用 `--info` 看日志范围；尝试去掉过滤或改用 `--frame-type all`。 |
| 大量 `skip bad line` | 日志格式不支持或行内容损坏 | 确认是 candump 格式；检查 `[8]` / `[08]` 和十六进制载荷。 |
| 回灌后收不到帧 | 目标接口、映射或回环设置不对 | 核对 `--iface` / `--map`；检查 `--no-loopback`；在另一终端 `candump` 验证。 |
| 真实总线出现错误帧或 bus-off | 没有其他节点 ACK、波特率不匹配或总线故障 | 立即停止发送，检查接线、终端电阻和位时序，隔离总线后再试。 |
| 菜单找不到回灌引擎 | `can_replay.py` 不在预期目录或版本过旧 | 把新版 `can_replay.py` 放到脚本同目录；旧版不支持 `--frame-type`。 |
| Tab 没有补全 | 当前输入不是交互式终端，或日志目录选错 | 在真实终端中运行菜单；确认步骤 1 的日志目录正确。 |

## 安全与限制

- 真实 CAN 口需预先配置仲裁波特率；CAN FD 还需配置数据域波特率和 `fd on`。
  本工具只负责回放，不负责配置硬件位时序。
- `vcan` 是虚拟接口，只适用于软件验证，不能代替真实 CAN 硬件链路测试。
- 发送真实总线前必须核对目标接口、方向、ID、报文内容和帧类型；错误报文可能
  影响台架设备或车辆。建议先在 `vcan` 上验证，再在隔离总线上小流量试灌。
- 真实 CAN 接口如果没有第二个节点 ACK，发送可能持续重传直到 bus-off。
- 日志时间戳只用于计算帧间隔；多文件回灌必须使用同一场录制的时钟基准。
- 只回放数据帧；CAN FD 的 BRS 可通过 `--fd-flags` 指定，ESI 不还原，错误帧和
  RTR 帧不在支持范围内。

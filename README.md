# CAN 回灌工具 — Ubuntu 台架发行版

## 发行内容

- `can_replay.py`：统一回灌引擎。使用 Linux SocketCAN 和 Python 标准库，不依赖 Windows、Vector 或第三方 Python 包。
- `can_replay_menu.sh`：终端交互菜单，可选日志、方向、目标 CAN 接口、速度和循环次数。

## 方向约定

本版本沿用当前工程的台架约定：

- 上行：candump `[08]` CAN FD 帧（其他节点发来、ADU 接收）。
- 下行：candump `[8]` 经典 CAN 帧（ADU 发送）。
- 全部：两种帧都发送。

这是当前台架的业务约定，不是 CAN 协议定义。方向选择只筛选日志帧，不会自动判定物理总线方向或修改报文内容；物理出口由所选 `canX` / `vcanX` 接口决定。

## Ubuntu 台架运行

将发行包复制并解压到 Ubuntu/Linux 台架，确认目标 SocketCAN 接口已配置、已启用，然后：

```bash
chmod +x can_replay.py can_replay_menu.sh
./can_replay_menu.sh
```

菜单中选择日志目录和文件、上行/下行/全部、目标接口及发送节奏。可先选择 `p` 预览，确认后再开始发送。向真实总线发送需要 root 权限，菜单会通过 `sudo` 提权。

### 命令行示例

```bash
# 预览上行 FD 帧，不发送
./can_replay.py --direction upstream --dry-run --stats candump.log

# 下行经典帧发到台架接口 can0
sudo ./can_replay.py --direction downstream --iface can0 candump.log

# 全部帧发到 can0，2 倍速，仅发送前 100 帧
sudo ./can_replay.py --direction both --iface can0 --speed 2 --limit 100 candump.log

# 查看日志概要
./can_replay.py --info candump.log
```

也可用 `--frame-type classic|fd|all`、`--filter-id`、`--from` / `--to`、`--map`、`--loop` 等选项；执行 `./can_replay.py --help` 查看完整参数。

## 系统要求和安全

- Ubuntu/Linux 内核支持 SocketCAN；Python 3 可用。
- 真实 CAN 口需预先按台架硬件配置好接口、仲裁波特率及 CAN FD 数据域波特率；本工具负责回放，不负责配置硬件波特率。
- `vcan` 是虚拟接口，只适用于软件验证，不能代替真实 CAN 硬件链路测试。
- 发送真实总线前必须核对目标接口、方向、ID、报文内容和帧类型；错误报文可能影响台架设备或车辆。建议先 dry-run，再在隔离总线上小流量验证。
- CAN FD 日志不包含完整 BRS/ESI 信息时，需按原始采集和台架设置确认；`--fd-flags` 可指定 BRS 等 FD 标志。

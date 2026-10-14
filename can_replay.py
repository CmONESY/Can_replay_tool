#!/usr/bin/env python3
"""把 candump 日志回灌到 SocketCAN 接口（Linux，纯标准库，无第三方依赖）。

支持 3 种常见日志行格式（可混用、可多文件合并按时间排序）：

    (2026-09-21 15:30:00.097618)  vcan2  181D20A0   [8]  01 00 00 00 64 00 64 64
    (1789975800.097618)  vcan2  181D20A0   [8]  01 00 00 00 64 00 64 64
    (1789975800.097618)  vcan2  181D20A0#0100000064006464

注意 candump 的长度写法区分帧类型，回灌时会原样还原：
    [8]  = 经典 CAN 2.0 帧（长度一位十进制）
    [08] = CAN FD 帧（长度两位十进制，0~64）
FD 帧在 candump 里不记录 BRS/ESI 标志位，需要时用 --fd-flags 指定（0~F）。

时间戳只用于计算帧间隔，因此一次回灌的多份日志必须来自同一场录制（时钟基准
一致）；多文件会按时间戳合并排序，例如同时回灌 vcan0/vcan2 两路日志。

典型用法：

    # 干跑：只解析、只打印，不开 socket、不发帧
    ./can_replay.py --dry-run --stats logs/candump_vcan2_*.log

    # 原速回灌到 vcan2（日志里的接口名）
    sudo ./can_replay.py logs/candump_vcan2_*.log

    # 把日志里的 vcan2 改灌到 vcan0，50 倍速，无限循环
    sudo ./can_replay.py --map vcan2=vcan0 --speed 50 --loop i logs/candump_vcan2_*.log

    # 只回灌底盘控制相关的 ID，并丢掉前 100 帧
    sudo ./can_replay.py --filter-id 181D20A0,181D21A0,181D22A0 --skip 100 logs/*.log

    # 只回灌 15:31:00 ~ 15:32:30 这段（支持 HH:MM:SS，或带日期）
    sudo ./can_replay.py --from 15:31:00 --to 15:32:30 logs/*.log
    sudo ./can_replay.py --from '2026-09-21 15:31:00' --to '2026-09-21 15:32:30' logs/*.log

    # 还原 FD 帧时带上 BRS 标志（原速灌到实车总线时更接近原始报文）
    sudo ./can_replay.py --fd-flags 1 logs/candump_vcan0_*.log

    # 只回灌 ADU 发送的经典帧（candump 的 [8]），跳过其他节点发来的 FD 帧（[08]）
    sudo ./can_replay.py --frame-type classic logs/candump_vcan0_*.log

注意：实车总线上回灌控制报文会影响车辆，务必先灌到 vcan 验证；真实 CAN
接口若没有第二个节点 ACK，发送会进入重传直至 bus-off。

--------------------------------------------------------------------
文件结构导航（按定义顺序，从上到下）：

    Frame                日志行解析出来的一帧，全程序唯一的数据结构
    parse_timestamp()    时间戳字符串 -> (epoch 秒, datetime)
    parse_time_bound()   --from / --to 参数 -> datetime 或 datetime.time
    make_time_filter()   生成时间段过滤闭包 keep() / beyond_end()
    parse_data()         十六进制载荷文本 -> bytes
    parse_line()         一行日志 -> Frame（核心解析器）
    load_frames()        多文件读取 -> 过滤 -> 排序 -> skip/limit
    encode()             Frame -> 内核 16 / 72 字节结构体
    open_socket()        建立并配置 SocketCAN raw socket
    format_frame()       单帧的人类可读文本（dry-run 打印用）
    parse_map()          --map 参数解析
    parse_filter()       --filter-id 参数解析
    scan_logs()          轻量扫描器，供 --info 与菜单出摘要
    print_stats()        每 ID 的 loaded / sent 统计
    main()               命令行入口：参数 -> 装载 -> dry-run 或发送循环
"""

from __future__ import annotations

# 只用标准库：台架机器不需要 pip 装任何东西，拷过去就能跑
import argparse
import datetime
import json
import re
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

# ------------------------------ 内核标志位 ------------------------------
# 这三个是内核 can_frame.can_id 里的高位标志：日志里一旦带上，解析时
# 要先从 ID 上剥掉，才能得到发送时真正要用的“裸 ID”。
CAN_EFF_FLAG = 0x80000000  # 扩展帧（29 位 ID）
CAN_ERR_FLAG = 0x20000000  # 错误帧（日志里出现时直接丢弃）

# CAN FD 帧独有的 flags 字节；candump 日志不记录这两位，所以 BRS 只能由
# --fd-flags 命令行参数补上，ESI 本工具不还原（恒为 0）。
CANFD_BRS = 0x01  # 数据域变速（Bit Rate Switch）
CANFD_ESI = 0x02  # 错误状态指示（Error State Indicator），暂未使用

# --------------------------- SocketCAN 常量 ---------------------------
# 老版本 Python 的 socket 模块里没有这些名字，这里给默认值（数值与内核一致）。
SOL_CAN_RAW = getattr(socket, "SOL_CAN_RAW", 101)
CAN_RAW_LOOPBACK = getattr(socket, "CAN_RAW_LOOPBACK", 3)
CAN_RAW_FD_FRAMES = getattr(socket, "CAN_RAW_FD_FRAMES", 5)

# ---------------------------- 内核结构体布局 ----------------------------
# 这两个布局必须和内核的 struct can_frame / struct canfd_frame 逐字节一致，
# 否则 send() 会返回 EINVAL，或者 ID/长度被解析到错误的位置。
#   CLASSIC_FRAME "=IB3x8s"   : 4 字节 ID + 1 字节长度 + 3 字节填充 + 8 字节数据 = 16 字节
#   FD_FRAME      "=IBBBB64s" : 4 字节 ID + 长度 + flags + res0 + res1 + 64 字节数据 = 72 字节
CLASSIC_FRAME = struct.Struct("=IB3x8s")    # 经典 CAN 2.0，固定 16 字节
FD_FRAME = struct.Struct("=IBBBB64s")       # CAN FD，固定 72 字节

# ------------------------------ 日志行正则 ------------------------------
# 解析分两步：先用 LINE 把整行切成「时间戳 / 接口名 / 其余部分」，
# 再用下面两条正则去解析「其余部分」。
# 帧内容部分：<id> [len] <bytes...>，或紧凑写法 <id>#<bytes> / <id>##<flags><bytes>
REST_SPACED = re.compile(
    r"^(?P<can_id>[0-9A-Fa-f]+)\s+\[(?P<dlc_text>[0-9A-Fa-f]{1,2})\]\s*(?P<data>.*)$"
)  # 空格式：181D20A0 [8] 01 00 ...；dlc_text 的位数决定经典帧还是 FD 帧
REST_COMPACT = re.compile(r"^(?P<can_id>[0-9A-Fa-f]+)#(?P<data>.*)$")  # 紧凑式：181D20A0#0100...
LINE = re.compile(
    r"""^\s*\(
        (?P<ts>
            [0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?   # 写法一：日期时间
            |
            [0-9]+(?:\.[0-9]+)?                                                      # 写法二：epoch 秒
        )
        \)\s+(?P<iface>\S+)\s+(?P<rest>.+?)\s*$""",  # 括号后：接口名 + 帧内容
    re.VERBOSE,  # VERBOSE 模式下空白与 # 注释都会被忽略，所以下面可以随便折行
)


#帧结构
@dataclass
class Frame:
    """一行日志解析出来的一帧。

    设计要点：把「日志里记了什么」和「发送需要什么」分开存 ——
    ts / dt 只服务于过滤和节奏，其余字段才是发帧真正用到的。
    """

    ts: float  # 秒（epoch），只用于排序和计算帧间隔
    dt: Optional[datetime.datetime]  # 原始时间戳，用于 --from/--to 时间段过滤
    iface: str  # 日志里记录的接口名，发送前可能被 --iface / --map 改写
    can_id: int  # 已剥掉 EFF/ERR 标志位的裸 ID
    data: bytes  # 载荷字节（经典帧 0~8 字节，FD 帧 0~64 字节）
    is_fd: bool = False  # 是否 CAN FD 帧（candump 里写成 [08] 或 ##flags）
    is_err: bool = False  # 是否错误帧（装载阶段直接丢弃，不进发送队列）
    brs: bool = False  # FD 帧的 BRS 位，来自 --fd-flags
    dlc: Optional[int] = None  # 日志里声明的长度，仅用于给短数据补零
    source: str = ""  # 来源文件名，报错/调试时定位用
    lineno: int = 0  # 源文件行号，同时作为同时间戳帧的排序兜底

    @property
    def eff(self) -> bool:
        """是否扩展帧（29 位 ID）：只读派生属性，不额外占字段。"""
        return self.can_id > 0x7FF  # 大于 0x7FF 即扩展帧，发送时再 OR 回 CAN_EFF_FLAG


#解析时间戳
def parse_timestamp(text: str) -> Tuple[float, datetime.datetime]:
    """时间戳文本 -> (epoch 秒, datetime)。

    返回两个值是因为两处用法不同：epoch 秒给排序和节奏用，
    datetime 给 --from/--to 过滤用。两条分支都按本机时区解释，
    所以「日志里的时间」和「命令行给的钟点」天然是同一基准。
    """
    text = text.strip()
    if "-" in text:  # 写法一：2026-09-21 15:30:00.097618
        # fromisoformat 要的是 T 分隔，这里把第一个空格换成 T
        dt = datetime.datetime.fromisoformat(text.replace(" ", "T", 1))
    else:  # 写法二：epoch 秒，如 1789975800.097618
        dt = datetime.datetime.fromtimestamp(float(text))
    return dt.timestamp(), dt


#解析命令行输入的时间
def parse_time_bound(text: str, option: str):
    """把 --from/--to 解析成 datetime 或 datetime.time。

    支持三种写法：
        16:25          -> 16 点 25 分（HH:MM，按钟点理解）
        16:25:30       -> 16 点 25 分 30 秒（HH:MM:SS）
        2026-09-21 16:25:30 -> 带日期
    """
    text = text.strip()
    if not text:  # 没给这个参数
        return None
    if "-" in text or "T" in text:  # 带日期 -> 返回 datetime
        return datetime.datetime.fromisoformat(text.replace(" ", "T", 1))
    # 只有钟点 -> 返回 datetime.time；下面按冒号段数区分 HH:MM 与 HH:MM:SS
    parts = text.split(":")
    try:
        if len(parts) == 2:  # HH:MM，秒补 0
            hour, minute, second = int(parts[0]), int(parts[1]), 0.0
        elif len(parts) == 3:  # HH:MM:SS(.fff)，秒允许是小数
            hour, minute, second = int(parts[0]), int(parts[1]), float(parts[2])
        else:
            raise ValueError
    except ValueError:
        # 报错信息里带上 option，一眼能看出是 --from 还是 --to 写错了
        raise SystemExit(f"{option} expects HH:MM / HH:MM:SS / 'YYYY-MM-DD HH:MM:SS', got {text!r}")
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second < 61):
        raise SystemExit(f"{option} 不是合法时间: {text!r}")
    # 秒的小数部分换算成微秒（datetime.time 只接受整数微秒）
    micro = int(round((second - int(second)) * 1_000_000))
    return datetime.time(hour, minute, int(second), micro)


#帧时间过滤器
def make_time_filter(lo, hi):
    """根据 --from/--to 生成两个闭包：keep() 逐帧判断，beyond_end() 提前收尾。

    lo / hi 可能是 datetime（带日期）也可能是 datetime.time（只有钟点），
    两者比较方式不同，所以在这里先把边界类型定下来，避免主循环反复判断类型。
    """
    if lo is None and hi is None:  # 两个都没给，不需要过滤
        return None, None
    # 只要有一边带了日期，就统一按 datetime 比较（钟点边界拼到该帧自己的日期上）
    both_datetime = isinstance(lo, datetime.datetime) or isinstance(hi, datetime.datetime)

    def keep(frame: Frame) -> bool:
        """这一帧是否落在 [lo, hi] 区间内。"""
        moment = frame.dt
        if moment is None:
            return False
        left, right = lo, hi
        if both_datetime:
            # 只有钟点的那一边，和帧自己的日期拼成完整 datetime 再比
            if isinstance(left, datetime.time):
                left = datetime.datetime.combine(moment.date(), left)
            if isinstance(right, datetime.time):
                right = datetime.datetime.combine(moment.date(), right)
        else:
            # 两边都是钟点：只比「时:分:秒」，忽略日期
            moment = moment.time()
        if left is not None and moment < left:
            return False
        if right is not None and moment > right:
            return False
        return True

    def beyond_end(frame: Frame) -> bool:
        """帧已经晚于 --to（日志按时间递增，可以直接停止扫描）。"""
        if hi is None or frame.dt is None:
            return False
        if isinstance(hi, datetime.datetime):
            return frame.dt > hi
        return frame.dt.time() > hi

    return keep, beyond_end


#帧内容转字节
def parse_data(text: str, is_fd: bool) -> bytes:
    """把 "01 00 00 00 64 00 64 64" / "0100000064006464" 这类文本转成 bytes。"""
    # 先抹掉各种分隔符：点、空格、下划线
    hexstr = text.replace(".", "").replace(" ", "").replace("_", "").strip()
    if not hexstr:  # 空载荷（长度 0 的帧）
        return b""
    if len(hexstr) % 2:  # 十六进制必须成对出现
        raise ValueError(f"odd number of hex digits: {text!r}")
    data = bytes.fromhex(hexstr)
    limit = 64 if is_fd else 8  # 长度上限：经典帧 8 字节，FD 帧 64 字节
    if len(data) > limit:
        raise ValueError(f"payload {len(data)} bytes exceeds {limit} byte limit")
    return data


#解析原始日志行为一帧数据
def parse_line(line: str, source: str, lineno: int, fd_flags: int = 0) -> Optional[Frame]:
    """把一行日志解析成 Frame。

    返回 None 表示「这行不是帧」（空行、注释、无关文本），安静跳过；
    抛 ValueError 表示「像帧但格式不对」，由 load_frames 打警告后跳过。
    """
    # 第 0 步：空行、以 # 开头的注释行，直接忽略
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    # 第 1 步：用 LINE 正则把整行切成「时间戳 / 接口名 / 帧内容」三块
    # canplayer 格式的裸行（无接口名）不支持，明确提示而不是静默丢弃
    match = LINE.match(line)
    if not match:
        if stripped.startswith("("):  # 有括号却解析不了 = 格式不认识，报出来
            raise ValueError(f"unrecognized frame line: {stripped!r}")
        return None  # 完全不像日志的行，静默跳过

    ts, dt = parse_timestamp(match.group("ts"))
    iface = match.group("iface")
    rest = match.group("rest").strip()

    # 帧属性先给默认值，下面两条解析分支各自覆盖
    is_fd = False
    is_err = False
    brs = False
    dlc: Optional[int] = None

    # 第 2 步：解析帧内容，先试紧凑式（带 #），不匹配再试空格式（带 [len]）
    compact = REST_COMPACT.match(rest)
    if compact:
        can_id = int(compact.group("can_id"), 16)
        tail = compact.group("data")  # 第一个 # 后面的全部内容
        if tail.startswith("#"):  # <id>##<flags><data>：第二个 # 表示 CAN FD
            is_fd = True
            if not tail[1:]:  # ## 后面连 flags 都没有，日志坏了
                raise ValueError(f"missing CAN FD flags in {rest!r}")
            flags = int(tail[1], 16)  # 取一位十六进制 flags
            brs = bool(flags & CANFD_BRS)
            data = parse_data(tail[2:], is_fd)  # 跳过 flags 位，其余是数据
        else:
            # 普通紧凑式 <id>#<data>；有的 candump 变体会在后面追加 "_" 开头的字段
            body = tail.split("_")[0]
            data = parse_data(body, is_fd)
    else:
        spaced = REST_SPACED.match(rest)
        if not spaced:
            raise ValueError(f"unrecognized frame body: {rest!r}")
        can_id = int(spaced.group("can_id"), 16)
        dlc_text = spaced.group("dlc_text")
        dlc = int(dlc_text, 10)
        # candump 用 [%d] 打经典帧、[%02d] 打 CAN FD 帧，长度位数就是帧类型
        is_fd = len(dlc_text) == 2
        body = spaced.group("data").strip()
        data = parse_data(body, is_fd)
        if is_fd:
            # candump 不记录 FD 的 BRS 位，只能用命令行的 --fd-flags 还原
            brs = bool(fd_flags & CANFD_BRS)
        if len(data) > dlc:  # 数据比声明的长度还长，日志坏了
            raise ValueError(f"payload {len(data)} bytes longer than declared length {dlc}")

    # 第 3 步：把 ID 高位上的内核标志位剥掉，只留发送时要用的裸 ID
    if can_id & CAN_ERR_FLAG:
        is_err = True
        can_id &= ~CAN_ERR_FLAG
    if can_id & CAN_EFF_FLAG:
        can_id &= ~CAN_EFF_FLAG

    # 第 4 步：candump 可能打印「声明长度大于实际数据」的帧，补零对齐声明长度
    if dlc is not None and len(data) < dlc and dlc <= 8:
        # 允许 candump 里 "8 字节载荷 + DLC 9..F" 之类的写法
        data = data.ljust(dlc, b"\x00")

    # 第 5 步：组装 Frame（source / lineno 留着报错定位和排序用）
    return Frame(
        ts=ts,
        dt=dt,
        iface=iface,
        can_id=can_id,
        data=data,
        is_fd=is_fd,
        is_err=is_err,
        brs=brs,
        dlc=dlc,
        source=source,
        lineno=lineno,
    )

#整合原can log为可用数据
def load_frames(paths: Iterable[str], filter_ids: Optional[set], skip: int,
                time_filter=None, fd_flags: int = 0, frame_type: str = "all",
                stats: Optional[dict] = None, beyond_end=None,
                limit: Optional[int] = None) -> List[Frame]:
    """多份日志 -> 过滤 -> 合并排序 -> 切片的唯一入口。

    过滤顺序：坏行跳过 -> 错误帧丢弃 -> 越过 --to 提前停 -> ID 白名单 ->
    时间段 -> 帧类型。返回的列表已按 (时间戳, 行号) 排序，并丢掉前 skip 帧。
    """
    frames: List[Frame] = []
    stats = stats if stats is not None else {}
    # 只有单文件时才敢边读边数、提前收工；多文件必须先全部读完再合并排序
    single_file = len(list(paths)) == 1 if not isinstance(paths, list) else len(paths) == 1
    stop_after = skip + limit if (limit and single_file) else None
    for path in paths:
        with open(path, "r", errors="replace") as handle:  # errors=replace：脏字符不炸
            for lineno, line in enumerate(handle, 1):
                try:
                    frame = parse_line(line, path, lineno, fd_flags)
                except ValueError as exc:
                    # 坏行不致命：往 stderr 打提示后继续读下一行
                    print(f"{path}:{lineno}: skip bad line: {exc}", file=sys.stderr)
                    continue
                if frame is None or frame.is_err:
                    continue  # 非帧行和错误帧都丢掉
                if beyond_end is not None and frames and beyond_end(frame):
                    break  # 日志按时间递增，后面只会更晚，本文件不用再读了
                if filter_ids and frame.can_id not in filter_ids:
                    continue  # --filter-id 白名单
                if time_filter and not time_filter(frame):
                    continue  # --from / --to 时间段
                if frame_type == "classic" and frame.is_fd:
                    stats["skipped_fd"] = stats.get("skipped_fd", 0) + 1
                    continue  # 只要经典帧：丢掉 FD 帧
                if frame_type == "fd" and not frame.is_fd:
                    stats["skipped_classic"] = stats.get("skipped_classic", 0) + 1
                    continue  # 只要 FD 帧：丢掉经典帧
                frames.append(frame)
                if stop_after is not None and len(frames) >= stop_after:
                    break  # 单文件 + --limit 时的性能捷径
    if not frames:
        return []
    # 多文件合并成一条时间线；同一时间戳用行号兜底，保证排序稳定
    frames.sort(key=lambda f: (f.ts, f.lineno))
    return frames[skip:]  # 排好序之后再丢掉前 skip 帧

#编码为socketcan可以用的数据
def encode(frame: Frame) -> bytes:
    """Frame -> 内核结构体字节串（经典帧 16 字节 / FD 帧 72 字节）。"""
    can_id = frame.can_id
    if frame.eff:  # 扩展帧要把 EFF 标志位 OR 回去
        can_id |= CAN_EFF_FLAG
    if frame.is_fd:
        # FD 帧：长度字段用真实数据长度，flags 只带 BRS（ESI 恒为 0）
        flags = CANFD_BRS if frame.brs else 0
        payload = frame.data[:64]
        return FD_FRAME.pack(can_id, len(payload), flags, 0, 0, payload)
    # 经典帧：数据补齐到 8 字节，长度字段仍用真实数据长度
    payload = frame.data[:8].ljust(8, b"\x00")
    return CLASSIC_FRAME.pack(can_id, len(frame.data), payload)

#打开socket接口
def open_socket(iface: str, loopback: bool, need_fd: bool) -> socket.socket:
    """按接口名建一个配置好的 SocketCAN raw socket。"""
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    if not loopback:
        sock.setsockopt(SOL_CAN_RAW, CAN_RAW_LOOPBACK, 0)  # 关回环：自己发的帧不再回给自己
    if need_fd:
        sock.setsockopt(SOL_CAN_RAW, CAN_RAW_FD_FRAMES, 1)  # 允许发 72 字节的 FD 帧
    sock.bind((iface,))  # 绑定到具体的 canX / vcanX 接口
    return sock

#预览模式
def format_frame(frame: Frame) -> str:
    """一帧的可读文本，给 --dry-run 预览用（[N] 是实际数据长度，不是 DLC）。"""
    ident = f"{frame.can_id:08X}" if frame.eff else f"{frame.can_id:03X}"  # 扩展帧 8 位，标准帧 3 位
    body = " ".join(f"{b:02X}" for b in frame.data)
    kind = "FD" if frame.is_fd else "2.0"
    return f"{ident} [{len(frame.data)}] {body} ({kind})"

#回灌接口设定
def parse_map(items: Iterable[str]) -> Dict[str, str]:
    """解析 --map 的 LOG_IFACE=DST_IFACE 列表，返回 {日志接口名: 目标接口名}。"""
    mapping: Dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--map expects LOG_IFACE=DST_IFACE, got {item!r}")
        src, dst = item.split("=", 1)
        mapping[src.strip()] = dst.strip()
    return mapping

#解析输入的帧ID
def parse_filter(items: Iterable[str]) -> Optional[set]:
    """解析 --filter-id：逗号或分号分隔、可重复给，返回十六进制 ID 集合。"""
    ids = set()
    for item in items or []:
        for token in item.replace(";", ",").split(","):  # 分号也当分隔符
            token = token.strip()
            if token:
                ids.add(int(token, 16))
    return ids or None  # 空集合返回 None，调用方用 if filter_ids 判断即可

#can log 快速预览
def scan_logs(paths: Iterable[str]) -> dict:
    """只统计不建帧对象，用于界面/脚本快速列出日志里的报文。

    注意这是第二套解析逻辑（用 str.split，不走向正），口径比 parse_line 松：
    不校验合法性、也不剔除错误帧，所以帧数可能略多于真正会被回灌的帧数。
    """
    total = 0
    ids: dict = {}  # can_id -> {"id", "count", "kind"（classic / fd / mixed）}
    ifaces: dict = {}  # 接口名 -> 帧数
    first_ts = last_ts = None
    per_file = []
    for path in paths:
        frames = 0
        file_first = file_last = None
        with open(path, "r", errors="replace") as handle:
            for line in handle:
                # 按空格切词，再看第一段是否以 ")" 结尾：
                # 结尾说明时间戳本身没空格（epoch 写法），否则是带日期的写法
                parts = line.split()
                if not parts or not parts[0].startswith("("):
                    continue
                if parts[0].endswith(")"):
                    # (1789975800.097618)  iface  rest...
                    timestamp = parts[0].strip("()")
                    iface = parts[1] if len(parts) > 1 else "?"
                    rest = parts[2:]
                else:
                    # (2026-09-21 15:30:00.097618)  iface  rest...（时间戳里含空格）
                    timestamp = f"{parts[0]} {parts[1]}".strip("()")
                    iface = parts[2] if len(parts) > 2 else "?"
                    rest = parts[3:]
                if not rest:
                    continue
                head = rest[0]
                if "#" in head:
                    # 紧凑式：ID 在 # 之前；出现两个 # 说明这行是 FD 帧
                    can_id = head.split("#", 1)[0].upper()
                    is_fd = "##" in head
                else:
                    # 空格式：ID 单独一段，下一段是 [len]，用位数判断帧类型
                    can_id = head.upper()
                    length_token = rest[1] if len(rest) > 1 else "[8]"
                    is_fd = len(length_token.strip("[]")) == 2
                kind = "fd" if is_fd else "classic"
                frames += 1
                # 按 ID 累计帧数；同一 ID 同时出现经典帧和 FD 帧就标成 mixed
                entry = ids.setdefault(can_id, {"id": can_id, "count": 0, "kind": kind})
                entry["count"] += 1
                if entry["kind"] != kind:
                    entry["kind"] = "mixed"
                ifaces[iface] = ifaces.get(iface, 0) + 1
                if file_first is None:
                    file_first = timestamp
                file_last = timestamp
        total += frames
        if file_first:
            first_ts = first_ts or file_first  # 整体首帧：取第一份文件的第一帧
            last_ts = file_last  # 整体末帧：取最后一份文件的最后一帧
        per_file.append({"path": path, "frames": frames,
                         "first_ts": file_first, "last_ts": file_last})
    # 时间跨度 = 末帧 - 首帧；时间戳解析不了就不给跨度
    span = None
    if first_ts and last_ts:
        try:
            span = parse_timestamp(last_ts)[0] - parse_timestamp(first_ts)[0]
        except (ValueError, OSError):
            span = None
    # 汇总结果，供 --info 文本输出与 --info-json 使用
    return {
        "frames": total,
        "span": span,
        "ids": sorted(ids.values(), key=lambda x: -x["count"]),  # 帧数多的排前面
        "ifaces": [{"name": k, "count": v} for k, v in sorted(ifaces.items(), key=lambda x: -x[1])],
        "first_ts": first_ts,
        "last_ts": last_ts,
        "files": per_file,
    }


def main() -> int:
    """命令行入口：解析参数 -> 装载日志 -> dry-run 或进入按原始节奏发送的循环。"""
    # ----------------------------- 0. 输出缓冲 -----------------------------
    # 输出被重定向到管道时默认按块缓冲，界面/日志会看不到实时进度
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    # --------------------------- 1. 命令行参数 ---------------------------
    # epilog 直接用模块 docstring，所以 --help 里能看到完整用法说明
    parser = argparse.ArgumentParser(
        description="Replay candump logs onto SocketCAN interfaces.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # 位置参数：日志文件，可给多个（按时间戳合并成一条时间线）
    parser.add_argument("logs", nargs="+", help="candump 日志文件（可多个，按时间合并）")
    # 发送目标：默认用日志里记的接口名；--iface 统一改写，--map 做逐接口映射
    parser.add_argument("--iface", help="统一发到这个接口（默认按日志里的接口名发送）")
    parser.add_argument("--map", action="append", default=[], metavar="LOG=DST",
                        help="接口映射，如 --map vcan2=vcan0，可重复")
    # 节奏控制：倍速、循环次数、相邻帧最小间隔
    parser.add_argument("--speed", type=float, default=1.0, help="回灌倍速，默认 1.0（原速）")
    parser.add_argument("--loop", default="1", help="循环次数，i/0 表示无限，默认 1")
    parser.add_argument("--gap-ms", type=float, default=0.0, help="相邻帧最小间隔（毫秒）")
    # 筛选：ID 白名单、时间段、跳过 / 限制帧数
    parser.add_argument("--filter-id", action="append", default=[],
                        help="只回灌这些 ID（十六进制，逗号分隔，可重复）")
    parser.add_argument("--from", dest="time_from", metavar="TIME",
                        help="只回灌该时刻之后的帧（HH:MM:SS 或 'YYYY-MM-DD HH:MM:SS'）")
    parser.add_argument("--to", dest="time_to", metavar="TIME",
                        help="只回灌该时刻之前的帧（同上）")
    parser.add_argument("--skip", type=int, default=0, help="跳过前 N 帧")
    parser.add_argument("--limit", type=int, default=0, help="最多回灌 N 帧（0 表示不限）")
    # 预览、回环与 FD 标志位：--dry-run 只打印不发送，--fd-flags 决定还原 FD 帧带哪些标志
    parser.add_argument("--dry-run", action="store_true", help="只解析打印，不建立 socket、不发帧")
    parser.add_argument("--no-loopback", action="store_true", help="关闭本地回环（不回灌给自己）")
    parser.add_argument("--fd-flags", default="0", metavar="HEX",
                        help="还原 CAN FD 帧时使用的 flags（0~F，1=BRS），默认 0")
    # 帧类型：classic = candump 的 [8]（ADU 发送），fd = candump 的 [08]（其他节点发来）
    parser.add_argument("--frame-type", choices=("all", "classic", "fd"), default="all",
                        help="只回灌指定帧类型：classic=candump 的 [8]（ADU 发送），"
                             "fd=candump 的 [08]（其他节点发来），默认 all")
    # --direction 是台架业务语义，会在下面翻译成 --frame-type
    parser.add_argument("--direction", choices=("upstream", "downstream", "both"),
                        help="按台架方向选择报文：upstream=[08] FD，downstream=[8] 经典帧，"
                             "both=两种都发；指定时覆盖 --frame-type")
    parser.add_argument("--classic-only", dest="frame_type", action="store_const",
                        const="classic", help="等价于 --frame-type classic")
    parser.add_argument("--fd-only", dest="frame_type", action="store_const",
                        const="fd", help="等价于 --frame-type fd")
    # 输出与行为开关
    parser.add_argument("--info", action="store_true",
                        help="只扫描并打印日志摘要（帧数/ID/时间范围），不回灌")
    parser.add_argument("--info-json", action="store_true", help="与 --info 配合，输出 JSON")
    parser.add_argument("--stats", action="store_true", help="结束时打印每个 ID 的帧数")
    parser.add_argument("--quiet", action="store_true", help="只在出错时输出")
    args = parser.parse_args()

    # ------------------------- 2. 参数归一化与校验 -------------------------
    # 方向语义按当前台架约定：上行使用日志中的 FD [08]，下行使用经典帧 [8]。
    # 方向属于业务分类，不是 CAN 协议字段；不同台架若定义不同，可改用 --frame-type。
    if args.direction:
        args.frame_type = {"upstream": "fd", "downstream": "classic", "both": "all"}[args.direction]

    # 倍速必须为正，否则下面的时间换算会除零或倒着走
    if args.speed <= 0:
        raise SystemExit("--speed must be > 0")
    # ------------------------- 2. 参数归一化与校验 -------------------------
    # 方向语义按当前台架约定：上行使用日志中的 FD [08]，下行使用经典帧 [8]。
    # 方向属于业务分类，不是 CAN 协议字段；不同台架若定义不同，可改用 --frame-type。
    if args.direction:
        args.frame_type = {"upstream": "fd", "downstream": "classic", "both": "all"}[args.direction]

    # 倍速必须为正，否则下面的时间换算会除零或倒着走
    if args.speed <= 0:
        raise SystemExit("--speed must be > 0")
    # ---------------------------- 3. --info 分支 ----------------------------
    # 只出摘要：走轻量扫描器，不建 Frame、不开 socket，也不关心过滤参数
    if args.info:
        summary = scan_logs(args.logs)
        if args.info_json:  # 给菜单/脚本读的机器可读格式
            print(json.dumps(summary, ensure_ascii=False))
            return 0
        # 人类可读格式：总览 -> 接口 -> 每个 ID 的帧数与帧类型
        print(f"文件数 {len(summary['files'])}, 总帧数 {summary['frames']}, "
              f"时间 {summary['first_ts']} ~ {summary['last_ts']}")
        for iface in summary["ifaces"]:
            print(f"  接口 {iface['name']}: {iface['count']} 帧")
        for item in summary["ids"]:
            mark = "经典" if item["kind"] == "classic" else ("FD" if item["kind"] == "fd" else "混合")
            print(f"  {item['id']}  [{mark}]  {item['count']}")
        return 0

    # --------------------------- 4. 组装过滤条件 ---------------------------
    mapping = parse_map(args.map)    # ------------------------- 2. 参数归一化与校验 -------------------------
    # 方向语义按当前台架约定：上行使用日志中的 FD [08]，下行使用经典帧 [8]。
    # 方向属于业务分类，不是 CAN 协议字段；不同台架若定义不同，可改用 --frame-type。
    if args.direction:
        args.frame_type = {"upstream": "fd", "downstream": "classic", "both": "all"}[args.direction]

    # 倍速必须为正，否则下面的时间换算会除零或倒着走
    if args.speed <= 0:
        raise SystemExit("--speed must be > 0")
    # 时间过滤返回两个闭包：keep 逐帧判断，beyond_end 让扫描提前收尾
    time_filter, beyond_end = make_time_filter(
        parse_time_bound(args.time_from, "--from") if args.time_from else None,
        parse_time_bound(args.time_to, "--to") if args.time_to else None,
    )
    # --fd-flags 用十六进制描述 FD 帧标志位（1 = BRS），范围 0~F
    try:
        fd_flags = int(args.fd_flags, 16)
    except ValueError:
        raise SystemExit(f"--fd-flags expects a hex digit 0~F, got {args.fd_flags!r}")
    if not 0 <= fd_flags <= 0xF:
        raise SystemExit("--fd-flags must be between 0 and F")

    # ----------------------------- 5. 装载日志 -----------------------------
    # 读取 + 过滤 + 排序都在 load_frames 里完成；stats 用来记录被帧类型过滤掉的数量
    stats: dict = {}
    frames = load_frames(args.logs, parse_filter(args.filter_id), args.skip, time_filter,
                         fd_flags, args.frame_type, stats, beyond_end,
                         args.limit if args.limit else None)
    if args.limit:
        frames = frames[: args.limit]  # 多文件时 load_frames 截不了，这里再补一刀
    if not frames:
        # 一帧都没有：若用了时间过滤，顺手把日志真实的时间范围提示出来
        hint = ""
        if args.time_from or args.time_to:
            try:
                summary = scan_logs(args.logs)
                hint = (f"日志时间范围是 {summary['first_ts']} ~ {summary['last_ts']}；"
                        f"注意 16:25 表示 16 点 25 分（HH:MM）")
            except OSError:
                hint = ""
        print("no frames to replay" + (f"（{hint}）" if hint else ""), file=sys.stderr)
        # 只灌经典帧却一帧都没有，通常是把 ADU 发送的报文过滤掉了
        if args.frame_type == "classic":
            print("提示: 当前只灌经典帧 [8]；若这段里没有 ADU 发送的报文，"
                  "可用 --frame-type all 或检查时间段", file=sys.stderr)
        return 1

    # --------------------------- 6. 改写目标接口 ---------------------------
    if args.iface:
        for frame in frames:  # --iface：所有帧统一改到一个接口
            frame.iface = args.iface
    for frame in frames:  # --map：按「日志里的接口名」逐个映射，因此优先级高于 --iface
        frame.iface = mapping.get(frame.iface, frame.iface)

    # --------------------------- 7. 发送前准备 ---------------------------
    sockets: Dict[str, socket.socket] = {}
    # 只要待发帧里有任意一帧是 FD，这个接口的 socket 就开 FD 模式（FD 能力是全局开关）
    need_fd = any(f.is_fd for f in frames)
    # 循环次数：i / 0 / inf / infinite 表示无限循环，否则至少跑 1 趟
    loop_forever = args.loop.strip().lower() in ("i", "0", "inf", "infinite")
    loops = float("inf") if loop_forever else max(1, int(args.loop))

    # 加载摘要：帧数、时间跨度、涉及接口，以及被帧类型过滤掉的帧数
    if not args.quiet:
        span = frames[-1].ts - frames[0].ts
        ifaces = sorted({f.iface for f in frames})
        extra = ""
        if stats.get("skipped_fd"):
            extra += f", skipped {stats['skipped_fd']} FD frames"
        if stats.get("skipped_classic"):
            extra += f", skipped {stats['skipped_classic']} classic frames"
        print(f"loaded {len(frames)} frames from {len(args.logs)} file(s), "
              f"span {span:.3f}s, interfaces {', '.join(ifaces)}"
              + (f", speed x{args.speed:g}" if args.speed != 1 else "") + extra)

    # --------------------------- 8. --dry-run 分支 ---------------------------
    # 只打印不发送：这里直接返回，所以永远不会建 socket，也就绝不会碰总线
    if args.dry_run:
        for frame in frames[:20]:  # 只看前 20 帧，避免刷屏
            print(f"{frame.ts:.6f} {frame.iface:>8} {format_frame(frame)}")
        if len(frames) > 20:
            print(f"... {len(frames) - 20} more frames")
        if args.stats:
            print_stats(frames)
        return 0

    # ----------------------------- 9. 发送循环 -----------------------------
    sent = 0
    counts: Dict[int, int] = {}  # 每个 ID 实际发出去的帧数，收尾统计用
    loop_index = 0
    try:
        while True:
            # 每一趟都重新对齐时间基准：让日志的首帧对应当下的时间
            base_log = frames[0].ts  # 日志时间轴原点
            base_wall = time.perf_counter()  # 墙上时钟原点
            previous_send = 0.0  # 上一帧的发送完成时刻，给 --gap-ms 用
            for frame in frames:
                # socket 按接口懒创建并缓存，同一接口只建一次
                if frame.iface not in sockets:
                    try:
                        sockets[frame.iface] = open_socket(
                            frame.iface, not args.no_loopback, need_fd
                        )
                    except OSError as exc:
                        print(f"cannot open {frame.iface}: {exc}", file=sys.stderr)
                        if exc.errno == 19:  # ENODEV：接口不存在，给一条现成的建接口命令
                            print("提示：接口不存在。回灌前先建虚拟总线："
                                  "sudo ip link add dev vcan0 type vcan && "
                                  "sudo ip link set vcan0 up", file=sys.stderr)
                        return 1

                # 目标时刻 = 本趟起点 + 原始时间差 / 倍速
                target = base_wall + (frame.ts - base_log) / args.speed
                if args.gap_ms > 0:
                    # --gap-ms 兜底：保证与上一帧至少隔开这么久
                    target = max(target, previous_send + args.gap_ms / 1000.0)
                delay = target - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)  # 没到点就睡；已过点就直接发，不做补偿也不丢帧

                sockets[frame.iface].send(encode(frame)) MNBNV C DESW 
                HGVF[ ,05]
                previous_send = time.perf_counter()  # 记下实际发完的时刻
                sent += 1
                counts[frame.can_id] = counts.get(frame.can_id, 0) + 1
            loop_index += 1
            # 无限循环或多趟循环时，每趟结束报一次进度
            if not args.quiet and (loops == float("inf") or loops > 1):
                print(f"pass {loop_index} done, replayed {sent} frames, "
                      f"elapsed {time.perf_counter() - base_wall:.1f}s")
            if loops != float("inf") and loop_index >= loops:
                break
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)  # Ctrl-C：不报错栈，安静收尾
    finally:
        for sock in sockets.values():
            sock.close()  # 正常结束、出错、中断都走这里关 socket

    # ---------------------------- 10. 收尾统计 ----------------------------
    if not args.quiet:
        print(f"replayed {sent} frames")
    if args.stats:
        print_stats(frames, counts)
    return 0


def print_stats(frames: List[Frame], sent: Optional[Dict[int, int]] = None) -> None:
    """打印每个 ID 的「装载帧数 / 实际发送帧数」对照表。"""
    parsed: Dict[int, int] = {}  # 每 ID 装载了多少帧
    for frame in frames:
        parsed[frame.can_id] = parsed.get(frame.can_id, 0) + 1
    print(f"{'ID':>10}  {'loaded':>8}  {'sent':>8}")  # 表头
    for can_id in sorted(parsed):  # 按 ID 从小到大列出
        ident = f"{can_id:08X}" if can_id > 0x7FF else f"{can_id:03X}"  # 扩展帧 8 位，标准帧 3 位
        print(f"{ident:>10}  {parsed[can_id]:>8}  "
              f"{(sent or {}).get(can_id, 0):>8}")


if __name__ == "__main__":
    # 入口：main() 的返回值直接作为进程退出码（0 成功，1 无帧可灌或建接口失败）
    sys.exit(main())

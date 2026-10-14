#!/usr/bin/env bash
# ============================================================
# CAN 回灌交互控制台（车端终端界面，无需图形环境）
#
# 交互式:  ./can_replay_menu.sh
# 命令行:  ./can_replay_menu.sh -d <日志目录> -f ALL -y -- --speed 50 --loop i
#
# 交互过程:
#   1) 选日志目录（支持 Tab 补全）
#   2) 选日志文件（ALL 全选 / 1,3 / 文件名）
#   3) 报文过滤：选择上行[08] FD / 下行[8]经典帧 / 全部，
#      时间段、指定报文、目标接口
#   4) 回灌节奏：倍速、循环、只灌前 N 帧
#   5) 确认后回灌；可以先输 p 预览，再确认开始
#
# 时间格式: 16:25 = 16 点 25 分（HH:MM）；16:25:30 = 16 点 25 分 30 秒（HH:MM:SS）
#           也接受 1625 / 162530 简写；写错会当场提示重填，不会把无效值传给回灌引擎
#
# 参数:
#   -d <目录>   日志目录（默认当前目录）
#   -f <选择>   直接选文件: ALL 或 1,3 或文件名
#   -y          跳过交互直接开始（回灌参数用 -- 后面的透传）
#   -- <参数>   其余参数原样传给 can_replay.py
#   -h          帮助
# ============================================================
set -uo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[1;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[错误]${NC} $1"; }
step()  { echo; echo -e "${BLUE}${BOLD}步骤 $1${NC} $2"; }
plain() { echo -e "${CYAN}$1${NC}"; }

PY=""
for cmd in python3 python; do
    command -v "$cmd" >/dev/null 2>&1 && { PY="$cmd"; break; }
done
[[ -n "$PY" ]] || { error "没找到 python3，本工具需要 python3（回灌走 SocketCAN）"; exit 1; }

# ─── 选择回灌引擎: 优先本目录，其次 can_tools/，要求支持 --frame-type ───
REPLAY=""
OLD_ENGINE=""
for cand in "$SCRIPT_DIR/can_replay.py" "$SCRIPT_DIR/can_tools/can_replay.py" \
            "$SCRIPT_DIR/../can_tools/can_replay.py" "$SCRIPT_DIR/../can_replay.py"; do
    [[ -f "$cand" ]] || continue
    if "$PY" "$cand" --help 2>/dev/null | grep -q -- '--frame-type'; then
        REPLAY="$(cd -P "$(dirname "$cand")" && pwd)/$(basename "$cand")"
        break
    fi
    [[ -z "$OLD_ENGINE" ]] && OLD_ENGINE="$cand"
done
if [[ -z "$REPLAY" ]]; then
    error "找不到可用的 can_replay.py（需要支持 --frame-type 的新版）"
    if [[ -n "$OLD_ENGINE" ]]; then
        error "检测到旧版本: $OLD_ENGINE"
        error "覆盖一下即可: cp $SCRIPT_DIR/can_tools/can_replay.py $OLD_ENGINE"
    else
        error "请把 can_replay.py 和本脚本放在同一目录，或放到 ./can_tools/ 下"
    fi
    exit 1
fi

LOGDIR=""
FILE_SEL=""
ASSUME_YES=0
EXTRA=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d) LOGDIR="${2:-}"; shift 2 ;;
        -f) FILE_SEL="${2:-}"; shift 2 ;;
        -y) ASSUME_YES=1; shift ;;
        --) shift; EXTRA=("$@"); break ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) warn "忽略未知参数: $1"; shift ;;
    esac
done

if [[ -n "$LOGDIR" && ! -d "$LOGDIR" ]]; then
    error "目录不存在: $LOGDIR"; exit 1
fi

CAN_IFACES=$(ip -br link show 2>/dev/null | awk '{print $1}' | grep -E '^(vcan|can|mcan)' | tr '\n' ' ')

echo -e "${BOLD}============================================================${NC}"
echo -e "${BOLD}            CAN 回灌交互控制台（车端）${NC}"
echo -e "${BOLD}============================================================${NC}"
info "主机 $(hostname) · $(uname -m) · $($PY -V 2>&1)"
info "回灌引擎: $REPLAY"
if [[ -n "$CAN_IFACES" ]]; then
    info "可用 CAN 接口: $CAN_IFACES"
else
    warn "当前没有 vcan/can 接口，可先创建: sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0"
fi

AT_EOF=0

PROMPT="$(printf '\033[1;36m▶\033[0m ')"   # 提问行标记，和输出区分开

ask() {   # ask <提示> <默认值> ; 结果放 REPLY
    local prompt="$PROMPT$1" default="${2:-}"
    if [[ -n "$default" ]]; then
        read -r -p "$prompt [$default]: " REPLY || { REPLY="$default"; AT_EOF=1; }
        [[ -z "$REPLY" ]] && REPLY="$default"
    else
        read -r -p "$prompt: " REPLY || { REPLY=""; AT_EOF=1; }
    fi
}

ask_path() {  # 带 Tab 补全的输入
    local prompt="$PROMPT$1" default="${2:-}"
    if [[ -n "$default" ]]; then
        read -e -r -p "$prompt [$default]: " REPLY || { REPLY="$default"; AT_EOF=1; }
        [[ -z "$REPLY" ]] && REPLY="$default"
    else
        read -e -r -p "$prompt: " REPLY || { REPLY=""; AT_EOF=1; }
    fi
}

ask_num() {   # ask_num <提示> <默认> [allow_i] -> 只能是整数（可选允许 i）
    local prompt="$1" default="$2" allow_i="${3:-}"
    while :; do
        ask "$prompt" "$default"
        if [[ "$REPLY" =~ ^[0-9]+$ ]]; then return 0; fi
        if [[ -n "$allow_i" && "${REPLY,,}" == "i" ]]; then return 0; fi
        warn "请输入数字${allow_i:+（或 i 表示无限）}: $REPLY"
        (( AT_EOF == 1 )) && { REPLY="$default"; return 0; }
    done
}

ask_positive() {  # ask_positive <提示> <默认> -> 正数（可小数）
    local prompt="$1" default="$2"
    while :; do
        ask "$prompt" "$default"
        if awk -v v="$REPLY" 'BEGIN{exit !(v+0 > 0)}' 2>/dev/null; then return 0; fi
        warn "请输入大于 0 的数: $REPLY"
        (( AT_EOF == 1 )) && { REPLY="$default"; return 0; }
    done
}

# ─── 步骤 1: 目录 ─────────────────────────────────────────
if [[ -z "$LOGDIR" ]]; then
    step 1/5 "选择日志目录"
    count_logs() { find "$1" -maxdepth 2 -type f \( -name '*.log' -o -name 'candump*' \) 2>/dev/null | wc -l; }
    cur_logs=$(count_logs "$PWD")
    echo "    1) 当前路径: $PWD   （$cur_logs 个日志）"
    echo "    2) 指定路径（输入时支持 Tab 补全）"
    while :; do
        # 当前目录没有日志时，默认引导去指定路径
        ask "选择" "$([[ "$cur_logs" -gt 0 ]] && echo 1 || echo 2)"
        case "$REPLY" in
            1) LOGDIR="$PWD" ;;
            *) ask_path "输入日志目录" "$PWD"; LOGDIR="${REPLY/#\~/$HOME}" ;;
        esac
        if [[ -d "$LOGDIR" ]] && [[ "$(count_logs "$LOGDIR")" -gt 0 ]]; then
            break
        fi
        if [[ ! -d "$LOGDIR" ]]; then
            warn "目录不存在: $LOGDIR"
        else
            warn "该目录（含子目录两层）里没找到 .log/candump* 日志: $LOGDIR"
        fi
        ask "重新选择？(y=重选 / n=退出)" "y"
        [[ "${REPLY,,}" == "n" || $AT_EOF -eq 1 ]] && exit 0
    done
    info "日志目录: $LOGDIR"
fi

# ─── 步骤 2: 文件 ─────────────────────────────────────────
mapfile -t FILES < <(find "$LOGDIR" -maxdepth 2 -type f \( -name '*.log' -o -name 'candump*' -o -name '*can*.log' \) 2>/dev/null | sort)
if [[ ${#FILES[@]} -eq 0 ]]; then
    error "目录里没找到日志: $LOGDIR"
    exit 1
fi

if [[ -z "$FILE_SEL" ]]; then
    step 2/5 "选择日志文件（支持 ALL / 1,3 / 直接输文件名）"
    i=0
    for f in "${FILES[@]}"; do
        i=$((i + 1))
        printf "  %2d) %-58s %s\n" "$i" "$(basename "$f")" "$(du -h "$f" | cut -f1)"
    done
    ask "选择" "ALL"
    FILE_SEL="$REPLY"
fi

SELECTED=()
if [[ "${FILE_SEL^^}" == "ALL" || -z "$FILE_SEL" ]]; then
    SELECTED=("${FILES[@]}")
else
    IFS=',' read -ra picks <<<"$FILE_SEL"
    for p in "${picks[@]}"; do
        p="$(echo "$p" | tr -d ' ')"
        [[ -z "$p" ]] && continue
        if [[ "$p" =~ ^[0-9]+$ ]]; then
            idx=$((p - 1))
            if (( idx >= 0 && idx < ${#FILES[@]} )); then
                SELECTED+=("${FILES[$idx]}")
            else
                warn "序号超范围: $p"
            fi
        else
            if [[ -f "$p" ]]; then
                SELECTED+=("$p")
            elif [[ -f "$LOGDIR/$p" ]]; then
                SELECTED+=("$LOGDIR/$p")
            else
                warn "找不到文件: $p"
            fi
        fi
    done
fi
[[ ${#SELECTED[@]} -gt 0 ]] || { error "没有选中任何日志"; exit 1; }
info "已选 ${#SELECTED[@]} 个文件"

# ─── 解析摘要（内置 awk，不依赖回灌引擎版本）────────────────
SUMMARY=$(awk '
function sod(t,   a, b) {
    split(t, a, ":"); split(a[3], b, ".")
    if (length(b[2]) == 0) b[2] = "0"
    while (length(b[2]) < 6) b[2] = b[2] "0"
    return a[1] * 3600 + a[2] * 60 + b[1] + ("0." b[2])
}
{
    line = $0
    sub(/^[ \t]+/, "", line)
    if (substr(line, 1, 1) != "(") next
    c = index(line, ")")
    if (c == 0) next
    ts = substr(line, 2, c - 2)
    rest = substr(line, c + 1)
    sub(/^[ \t]+/, "", rest)
    if (rest == "") next
    split(rest, f, /[ \t]+/)
    iface = f[1]; second = f[2]
    if (iface == "" || second == "") next
    if (index(second, "#") > 0) {
        id = second; sub(/#.*/, "", id); fd = (index(second, "##") > 0)
    } else {
        id = second; len = f[3]; gsub(/[\[\]]/, "", len); fd = (length(len) == 2)
    }
    id = toupper(id)
    total++
    cnt[id]++
    if (fd) isfd[id] = 1; else iscls[id] = 1
    icnt[iface]++
    if (first == "") first = ts
    last = ts
    if (index(ts, " ") > 0) {
        split(ts, d, " ")
        t = sod(d[2])
        if (f_sod == "") f_sod = t
        l_sod = t
    }
}
END {
    span = (f_sod != "") ? l_sod - f_sod : 0
    if (span < 0) span += 86400
    n = 0
    for (id in cnt) ids[++n] = id
    for (i = 1; i <= n; i++)
        for (j = i + 1; j <= n; j++)
            if (cnt[ids[j]] > cnt[ids[i]]) { tmp = ids[i]; ids[i] = ids[j]; ids[j] = tmp }
    cls = ""; fdl = ""; mix = ""
    for (i = 1; i <= n; i++) {
        id = ids[i]
        if (isfd[id] && iscls[id]) mix = mix (mix == "" ? "" : ",") id
        else if (isfd[id]) fdl = fdl (fdl == "" ? "" : ",") id
        else cls = cls (cls == "" ? "" : ",") id
    }
    ifs = ""
    for (k in icnt) ifs = ifs (ifs == "" ? "" : " ") k
    counts = ""
    for (i = 1; i <= n; i++)
        counts = counts (counts == "" ? "" : ",") ids[i] ":" cnt[ids[i]]
    printf "%d|%s|%s|%.1f|%s|%s|%s|%s|%s\n", total, first, last, span, ifs,
           (cls == "" ? "-" : cls), (fdl == "" ? "-" : fdl), mix, counts
}' "${SELECTED[@]}" 2>/dev/null)

if [[ -z "$SUMMARY" ]]; then
    error "解析日志失败（文件是否为空或格式不对？）"; exit 1
fi
IFS='|' read -r TOTAL FIRST LAST SPAN IFACES IDS_CLASSIC IDS_FD IDS_MIX IDS_COUNTED <<<"$SUMMARY"

# ID -> 帧数 查表（显示候选报文时用）
declare -A IDCNT=()
IFS=',' read -ra _pairs <<<"$IDS_COUNTED"
for _p in "${_pairs[@]}"; do
    [[ -n "$_p" ]] && IDCNT["${_p%%:*}"]="${_p##*:}"
done
echo
plain "  日志摘要: $TOTAL 帧 · 接口 $IFACES"
plain "  时间范围: ${FIRST// /_} ~ ${LAST// /_}"
plain "  原始时长: ${SPAN}s（回灌 1 倍速就要这么久）"
plain "  ADU 发送 [8]:  ${IDS_CLASSIC:0:150}"
plain "  ADU 接收 [08]: $(echo "$IDS_FD" | cut -c1-110)"
[[ -n "$IDS_MIX" ]] && plain "  两种格式都出现过: ${IDS_MIX:0:110}"

# ─── 步骤 3/4: 过滤与节奏（定义成函数，方便改参数重跑）──────
FRAME_TYPE="classic"; TIME_FROM=""; TIME_TO=""; ID_FILTER=""
MAPS=""; IFACE_INPUT=""; SPEED="1"; LOOP="1"; LIMIT="0"

frame_type_text() {
    case "$FRAME_TYPE" in
        classic) echo "下行：candump [8]（经典帧，ADU 发送）" ;;
        fd)      echo "上行：candump [08]（FD 帧，ADU 接收）" ;;
        *)       echo "全部 [8] + [08]" ;;
    esac
}

# ─── 时间输入: 校验 + 归一化（写错就重问，不让无效值流到回灌引擎）───
# 接受 16:25 / 16:25:30 / 16:25:30.5 / 1625 / 162530 / '2026-09-21 16:25:30'
# 成功: 打印归一化后的时间（带日期的原样保留）并返回 0；无效: 返回 1，不打印
norm_time() {
    local raw="$1" date="" rest h m s sec frac
    raw="${raw#"${raw%%[![:space:]]*}"}"            # 去左侧空白
    raw="${raw%"${raw##*[![:space:]]}"}"            # 去右侧空白
    [[ -z "$raw" ]] && { printf '\n'; return 0; }   # 空 = 不限

    if [[ "$raw" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})[[:space:]]+(.+)$ ]]; then
        date="${BASH_REMATCH[1]} "; rest="${BASH_REMATCH[2]}"
    else
        rest="$raw"
    fi
    rest="${rest//[[:space:]]/}"

    # 纯数字简写: 1625 -> 16:25，162530 -> 16:25:30
    case "$rest" in
        [0-9][0-9][0-9][0-9])           rest="${rest:0:2}:${rest:2:2}" ;;
        [0-9][0-9][0-9][0-9][0-9][0-9]) rest="${rest:0:2}:${rest:2:2}:${rest:4:2}" ;;
    esac

    IFS=':' read -r h m s <<<"$rest"
    [[ "$h" =~ ^[0-9]{1,2}$ && "$m" =~ ^[0-9]{1,2}$ ]] || return 1
    (( 10#$h <= 23 && 10#$m <= 59 )) || return 1
    if [[ -z "$s" ]]; then
        printf '%s%02d:%02d\n' "$date" "$((10#$h))" "$((10#$m))"
        return 0
    fi
    [[ "$s" =~ ^[0-9]{1,2}(\.[0-9]{1,6})?$ ]] || return 1
    sec="${s%%.*}"; frac=""
    [[ "$s" == *.* ]] && frac=".${s#*.}"
    (( 10#$sec <= 59 )) || return 1
    printf '%s%02d:%02d:%02d%s\n' "$date" "$((10#$h))" "$((10#$m))" "$((10#$sec))" "$frac"
}

drop_date() {    # '2026-09-21 16:25:30' -> '16:25:30'
    if [[ "$1" == *" "* ]]; then echo "${1##* }"; else echo "$1"; fi
}

hms_seconds() {  # HH:MM[:SS[.fff]] -> 当天秒数；无效返回 1
    local t="$1" h m s                # 按秒数比较，避免字符串比较受 locale 排序影响
    [[ -n "$t" ]] || return 1
    IFS=':' read -r h m s <<<"$t"
    [[ "$h" =~ ^[0-9]{1,2}$ && "$m" =~ ^[0-9]{1,2}$ ]] || return 1
    s="${s:-0}"; s="${s%%.*}"
    [[ "$s" =~ ^[0-9]{1,2}$ ]] || return 1
    echo $((10#$h * 3600 + 10#$m * 60 + 10#$s))
}

LOG_START_HMS="${FIRST#* }"; LOG_START_HMS="${LOG_START_HMS%.*}"
LOG_END_HMS="${LAST#* }";     LOG_END_HMS="${LOG_END_HMS%.*}"
# 日志时间是 epoch 数值时这两个为空，此时跳过与日志范围的重叠检查
LOG_START_SEC="$(hms_seconds "$LOG_START_HMS")"
LOG_END_SEC="$(hms_seconds "$LOG_END_HMS")"

TIME_RAW=""
ask_time() {   # ask_time <提示>；格式不对就重问，结果放 TIME_RAW（空 = 不限）
    local prompt="$1"
    while :; do
        ask "$prompt" ""
        [[ -z "$REPLY" ]] && { TIME_RAW=""; return 0; }
        if TIME_RAW="$(norm_time "$REPLY")"; then return 0; fi
        warn "时间格式不对: $REPLY"
        warn "可用写法: 16:25 / 16:25:30 / 1625 / 162530 / '2026-09-21 16:25:30'（直接回车=不限）"
        (( AT_EOF == 1 )) && { TIME_RAW=""; return 0; }
    done
}

ask_filter() {
    step 3/5 "报文过滤"
    echo "  方向约定：上行 = candump [08] FD（ADU 接收）；下行 = candump [8] 经典帧（ADU 发送）"
    echo "  1) 上行报文（FD [08]）    2) 下行报文（经典帧 [8]）    3) 上下行全部"
    while :; do
        ask "选择回灌方向" "2"
        case "$REPLY" in
            1|up|UP|upstream) FRAME_TYPE="fd"; break ;;
            2|down|DOWN|downstream) FRAME_TYPE="classic"; break ;;
            3|both|BOTH|all|ALL) FRAME_TYPE="all"; break ;;
            *) warn "请输入 1（上行）、2（下行）或 3（全部）" ;;
        esac
        (( AT_EOF == 1 )) && { FRAME_TYPE="classic"; break; }
    done
    while :; do
        ask_time "起始时间（回车=从日志开头；16:25 或 1625 表示 16 点 25 分）"
        TIME_FROM="$TIME_RAW"
        ask_time "结束时间（回车=到日志结尾；16:27 或 1627 表示 16 点 27 分）"
        TIME_TO="$TIME_RAW"
        [[ -z "$TIME_FROM" && -z "$TIME_TO" ]] && break
        local f_sec n_sec problem=""
        f_sec="$(hms_seconds "$(drop_date "$TIME_FROM")")"
        n_sec="$(hms_seconds "$(drop_date "$TIME_TO")")"
        if [[ -n "$f_sec" && -n "$n_sec" && "$f_sec" -ge "$n_sec" ]]; then
            problem="结束时间（$TIME_TO）要晚于起始时间（$TIME_FROM）"
        elif [[ -n "$f_sec" && -n "$LOG_END_SEC" && "$f_sec" -gt "$LOG_END_SEC" ]] \
          || [[ -n "$n_sec" && -n "$LOG_START_SEC" && "$n_sec" -lt "$LOG_START_SEC" ]]; then
            problem="时间段 ${TIME_FROM:-开头} ~ ${TIME_TO:-结尾} 与日志范围 $LOG_START_HMS ~ $LOG_END_HMS 不重叠"
        fi
        [[ -z "$problem" ]] && break
        warn "$problem"
        warn "日志范围: $LOG_START_HMS ~ $LOG_END_HMS（时间写法: 16:25 / 16:25:30 / 1625）"
        ask "重新填写？(y=重填 / n=保持并继续)" "y"
        [[ "${REPLY,,}" == "n" || $AT_EOF -eq 1 ]] && break
    done
    # 按选定的报文方向，列出可选报文供编号选择
    case "$FRAME_TYPE" in
        classic) CAND_LIST="$IDS_CLASSIC" ;;
        fd)      CAND_LIST="$IDS_FD" ;;
        *)       CAND_LIST="$(echo "${IDS_CLASSIC},${IDS_FD},${IDS_MIX}" | tr ',' '\n' \
                              | grep -v '^-*$' | grep -v '^$' | awk '!seen[$0]++' | paste -sd, -)" ;;
    esac
    CAND_IDS=()
    if [[ -n "$CAND_LIST" ]]; then
        # FF00 后缀单独一组；组内按 ID（即 FF00 组的前缀）升序，不按帧数排序。
        # 先排序再编号，保证显示编号与实际选择使用同一数组。
        mapfile -t CAND_IDS < <(printf '%s\n' "$CAND_LIST" | tr ',' '\n' \
            | awk '/^[0-9A-F]+$/ { print ($0 ~ /FF00$/ ? 0 : 1), $0 }' \
            | LC_ALL=C sort -k1,1n -k2,2 | awk '{ print $2 }')
        echo "  可选报文（$(frame_type_text)，共 ${#CAND_IDS[@]} 个）:"
        local group="" next_group col=0
        i=0
        for id in "${CAND_IDS[@]}"; do
            if [[ "$id" == *FF00 ]]; then next_group="FF00 后缀"; else next_group="其他报文"; fi
            if [[ "$next_group" != "$group" ]]; then
                (( col % 3 != 0 )) && echo
                echo "  ── $next_group（按 ID 前缀升序）──"
                group="$next_group"; col=0
            fi
            i=$((i + 1))
            col=$((col + 1))
            printf "    %2d) %-10s×%-6s" "$i" "$id" "${IDCNT[$id]:-?}"
            (( col % 3 == 0 )) && echo
        done
        (( col % 3 != 0 )) && echo
        echo "  多选可用空格或逗号隔开，如: 1 3 5   或   181E1F39 182E1F39"
    fi
    while :; do
        ask "只灌指定报文（回车=全部）" ""
        if [[ -z "$REPLY" || "${REPLY^^}" == "ALL" ]]; then
            ID_FILTER=""
            break
        fi
        picked=(); bad=0
        # 空格 / 逗号 / 分号 / 顿号 都当分隔符
        for t in ${REPLY//[,;、]/ }; do
            [[ -z "$t" ]] && continue
            if [[ "$t" =~ ^[0-9]+$ ]]; then
                idx=$((t - 1))
                if (( idx >= 0 && idx < ${#CAND_IDS[@]} )); then
                    picked+=("${CAND_IDS[$idx]}")
                    continue
                fi
                if [[ ${#t} -ne 3 && ${#t} -ne 8 ]]; then
                    warn "序号超范围: $t（当前方向共 ${#CAND_IDS[@]} 个）"
                    bad=1
                    continue
                fi
                # 3/8 位的纯数字也可能是报文 ID，按 ID 处理
            fi
            if [[ "$t" =~ ^[0-9A-Fa-f]{3}$ || "$t" =~ ^[0-9A-Fa-f]{8}$ ]]; then
                picked+=("${t^^}")
            else
                warn "无效输入: $t（应为序号，或 3 位/8 位十六进制 ID）"
                bad=1
            fi
        done
        if (( bad )); then
            ask "重新填写？(y=重填 / n=改为全部)" "y"
            [[ "${REPLY,,}" == "n" || $AT_EOF -eq 1 ]] && { ID_FILTER=""; break; }
            continue
        fi
        if [[ ${#picked[@]} -gt 0 ]]; then
            ID_FILTER="$(IFS=,; echo "${picked[*]}")"
            plain "  → 只灌: $ID_FILTER"
        else
            ID_FILTER=""
        fi
        break
    done
    local def_iface="$IFACES"     # 日志里的接口名，如 "vcan0" 或 "vcan0 vcan2"
    while :; do
        ask "目标接口" "$def_iface"
        IFACE_INPUT="$REPLY"
        # 回车（=默认值）表示按日志里的接口名灌，不额外加 --iface
        [[ "$IFACE_INPUT" == "$def_iface" ]] && IFACE_INPUT=""
        [[ -z "$IFACE_INPUT" ]] && break
        if [[ "$IFACE_INPUT" == *=* ]]; then
            MAPS="$IFACE_INPUT"
            IFACE_INPUT=""
            break
        fi
        if [[ " $CAN_IFACES " != *" $IFACE_INPUT "* ]]; then
            warn "接口 $IFACE_INPUT 当前不存在（本机现有: ${CAN_IFACES:-无}）"
            warn "要创建可执行: sudo ip link add dev $IFACE_INPUT type vcan && sudo ip link set up $IFACE_INPUT"
            ask "重新填写？(y=重填 / n=保持并继续)" "y"
            [[ "${REPLY,,}" == "n" || $AT_EOF -eq 1 ]] && break
            continue
        fi
        break
    done
}

ask_rhythm() {
    step 4/5 "回灌节奏"
    echo "  倍速        : 2 表示按 2 倍速快放；0.5 表示放慢一半"
    echo "  循环次数    : 这批日志重复灌几遍，i 表示一直循环"
    echo "  只灌前 N 帧 : 只发日志最前面的 N 帧，用来快速验证链路；0 = 整段全都灌"
    echo "  （每项直接回车 = 用中括号里的默认值）"
    ask_positive "倍速" "$SPEED";                       SPEED="$REPLY"
    ask_num "循环次数（数字或 i=无限）" "$LOOP" i;        LOOP="$REPLY"
    ask_num "只灌前 N 帧（0=整段全部）" "$LIMIT";         LIMIT="$REPLY"
}

if (( ASSUME_YES == 0 )); then
    ask_filter
    ask_rhythm
fi

build_cmd() {   # build_cmd <dry_run:0|1>
    CMD=("$PY" -u "$REPLAY")
    [[ "$FRAME_TYPE" != "all" ]] && CMD+=(--frame-type "$FRAME_TYPE")
    [[ -n "$TIME_FROM" ]] && CMD+=(--from "$TIME_FROM")
    [[ -n "$TIME_TO" ]] && CMD+=(--to "$TIME_TO")
    [[ -n "$ID_FILTER" ]] && CMD+=(--filter-id "$ID_FILTER")
    if [[ -n "$MAPS" ]]; then
        for m in $MAPS; do CMD+=(--map "$m"); done
    elif [[ -n "$IFACE_INPUT" ]]; then
        CMD+=(--iface "$IFACE_INPUT")
    fi
    CMD+=(--speed "$SPEED" --loop "$LOOP")
    [[ "$LIMIT" != "0" && -n "$LIMIT" ]] && CMD+=(--limit "$LIMIT")
    if [[ ${#EXTRA[@]} -gt 0 ]]; then CMD+=("${EXTRA[@]}"); fi
    CMD+=(--stats)
    if [[ "$1" == "1" ]]; then
        CMD+=(--dry-run)
        [[ "$LIMIT" == "0" || -z "$LIMIT" ]] && CMD+=(--limit 40)
    fi
    CMD+=("${SELECTED[@]}")
}

needs_root() { [[ "$1" != "1" ]]; }
sudo_prefix() {
    if sudo -n true 2>/dev/null; then echo "sudo -n"
    else echo "sudo"; fi
}

run_replay() {   # run_replay <dry_run:0|1>
    local dry="$1" prefix=()
    build_cmd "$dry"
    if needs_root "$dry"; then
        read -r -a prefix <<<"$(sudo_prefix)"
    fi
    echo
    plain "  执行: ${prefix[*]:+${prefix[*]} }${CMD[*]}"
    echo
    trap - INT TERM
    "${prefix[@]}" "${CMD[@]}"
    local rc=$?
    trap 'echo; info "已中断"; break' INT TERM
    if [[ $rc -eq 0 ]]; then info "回灌结束"; else warn "退出码 $rc（130 通常是手动中断）"; fi
    return $rc
}

while true; do
    if (( AT_EOF == 1 )); then
        info "输入结束（EOF），退出"
        exit 0
    fi
    if (( ASSUME_YES == 0 )); then
        step 5/5 "确认"
        cat <<EOF
  日志文件   : ${#SELECTED[@]} 个（$(basename "${SELECTED[0]}")${SELECTED[1]:+ 等}）
  报文方向   : $(frame_type_text)
  时间段     : ${TIME_FROM:-不限} ~ ${TIME_TO:-不限}
  报文过滤   : ${ID_FILTER:-全部}
  目标接口   : $([[ -n "$MAPS" ]] && echo "$MAPS" || echo "${IFACE_INPUT:-按日志接口名（$IFACES）}")
  倍速/循环  : ${SPEED} / ${LOOP}
  只灌前 N 帧: ${LIMIT}（0 = 整段全部）
  预计耗时   : $(awk -v s="$SPAN" -v v="$SPEED" -v l="$LOOP" -v n="$LIMIT" -v f="$TOTAL" 'function fmt(x){
        if (x < 1) return sprintf("%.0fms", x * 1000)
        if (x < 60) return sprintf("%.1fs", x)
        return sprintf("%.1fmin", x / 60) }
      BEGIN{
        if (v <= 0) v = 1
        dur = s / v
        if (n > 0 && s > 0 && f > 0) dur = (s * n / f) / v
        if (l == "i" || l == "0") printf "%s/轮（无限循环）", fmt(dur)
        else printf "%s", fmt(dur * (l + 0)) }')
EOF
        ask "开始回灌？(y=开始 / p=先预览 / n=退出)" "y"
        if (( AT_EOF == 1 )); then
            info "输入结束（EOF），已取消，避免误回灌"
            exit 0
        fi
        case "${REPLY,,}" in
            p) run_replay 1; echo; read -r -p "按回车继续（回到确认）" _ || true; continue ;;
            n) info "已取消"; exit 0 ;;
            *) ;;
        esac
    else
        step 5/5 "确认"
        echo "  -y 已指定，直接开始"
    fi

    run_replay 0
    # 跑完给三个选项，回车默认退出
    while :; do
        echo
        echo "  接下来:"
        echo "    r) 用同样参数再灌一遍"
        echo "    c) 改参数（回到报文过滤）"
        echo "    q) 退出"
        ask "选择" "q"
        if (( AT_EOF == 1 )); then
            exit 0
        fi
        case "${REPLY,,}" in
            r) run_replay 0 ;;
            c) break ;;
            *) exit 0 ;;
        esac
    done
    ask_filter
    ask_rhythm
done

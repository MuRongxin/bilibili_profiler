"""
CRC32 彩虹表：mid_hash → UID 毫秒级反查

表结构：8 字节定长记录 (crc32:uint32, uid:uint32)，struct '<II' 小端，
全表按 (crc32, uid) 升序排序。查询时 mmap 只读映射 + 手写二分定位，
碰撞（同 crc32 多个 uid）的记录相邻，二分后向两侧扫描收集全部候选。

构建流程：多进程分片计算 zlib.crc32(str(uid).encode())，片内排序写临时文件
data/crc_tmp_N.bin，再 heapq.merge 做 k 路归并流式写入 CRC_TABLE_PATH，
最后清理临时文件。纯标准库实现，不引入第三方依赖。
"""
import array
import heapq
import mmap
import multiprocessing
import os
import struct
import sys
import zlib

from config import CRC_TABLE_PATH, CRC_TABLE_MAX_UID, CRC_BUILD_CHUNK

_RECORD = struct.Struct("<II")   # (crc32, uid) 小端定长记录
RECORD_SIZE = _RECORD.size       # 8 字节

# 归并/写出缓冲：每 100 万条记录（8MB）落一次盘，控制内存
_FLUSH_RECORDS = 1_000_000

# 进程级 mmap 缓存：{表路径: (文件对象, mmap对象)}，避免重复打开
_mmap_cache: dict = {}


def _get_table_path() -> str:
    """返回当前生效的表路径（运行期读 config，便于测试 monkeypatch）"""
    import config
    return config.CRC_TABLE_PATH


def _get_tmp_dir() -> str:
    """临时分片文件目录（与表文件同目录）"""
    return os.path.dirname(_get_table_path()) or "."


def _build_chunk(task):
    """worker：计算 [start, end) 的 crc32，按 (crc32, uid) 排序后写临时分片文件"""
    start, end, tmp_path = task
    records = [(zlib.crc32(str(uid).encode()), uid) for uid in range(start, end)]
    records.sort()
    buf = array.array("I")
    for crc, uid in records:
        buf.append(crc)
        buf.append(uid)
    if sys.byteorder == "big":
        buf.byteswap()  # 统一按小端落盘
    with open(tmp_path, "wb") as f:
        buf.tofile(f)
    return tmp_path, len(records)


def _iter_chunk_file(path):
    """流式读取一个有序分片文件，逐条产出 (crc32, uid)"""
    buf = array.array("I")
    with open(path, "rb") as f:
        while True:
            data = f.read(_FLUSH_RECORDS * RECORD_SIZE)
            if not data:
                break
            buf.frombytes(data)
            if sys.byteorder == "big":
                buf.byteswap()
            for i in range(0, len(buf), 2):
                yield (buf[i], buf[i + 1])
            del buf[:]


def _flush_buffer(f, buf: array.array):
    """把输出缓冲落盘并清空"""
    if not buf:
        return
    out = array.array("I", buf)
    if sys.byteorder == "big":
        out.byteswap()
    out.tofile(f)
    del buf[:]


def build_table(max_uid: int = CRC_TABLE_MAX_UID, workers: int = None) -> bool:
    """
    构建彩虹表（一次性，带进度打印），返回是否成功。
    多进程分片（每片 CRC_BUILD_CHUNK 条）并行计算 + 片内排序，
    再 k 路归并流式写出最终表文件；失败时清理半成品返回 False。
    """
    if workers is None:
        workers = os.cpu_count() or 1
    table_path = _get_table_path()
    tmp_dir = _get_tmp_dir()
    os.makedirs(tmp_dir, exist_ok=True)

    # 分片任务：[i*chunk, (i+1)*chunk)
    chunk = CRC_BUILD_CHUNK
    tasks = []
    start = 0
    idx = 0
    while start < max_uid:
        end = min(start + chunk, max_uid)
        tasks.append((start, end, os.path.join(tmp_dir, f"crc_tmp_{idx}.bin")))
        start = end
        idx += 1

    tmp_paths = [t[2] for t in tasks]
    print(f"[彩虹表] 开始构建：UID 范围 0 ~ {max_uid - 1}，共 {len(tasks)} 片，{workers} 进程")

    try:
        # 1) 多进程分片计算
        done = 0
        with multiprocessing.Pool(processes=workers) as pool:
            for tmp_path, count in pool.imap_unordered(_build_chunk, tasks):
                done += 1
                print(f"[彩虹表] 分片完成 {done}/{len(tasks)}（{count} 条）", flush=True)

        # 2) k 路归并，流式写出（不全量入内存）
        print("[彩虹表] 开始归并写出 ...", flush=True)
        out_buf = array.array("I")
        written = 0
        partial_path = table_path + ".partial"
        with open(partial_path, "wb") as f:
            for crc, uid in heapq.merge(*[_iter_chunk_file(p) for p in tmp_paths]):
                out_buf.append(crc)
                out_buf.append(uid)
                written += 1
                if len(out_buf) >= _FLUSH_RECORDS * 2:
                    _flush_buffer(f, out_buf)
                    print(f"[彩虹表] 已归并 {written} 条", flush=True)
            _flush_buffer(f, out_buf)
        os.replace(partial_path, table_path)  # 原子替换，避免半成品被当作正式表
        print(f"[彩虹表] 构建完成：{table_path}（{written} 条，"
              f"{os.path.getsize(table_path) / 1024 / 1024:.1f} MB）")
        return True
    except Exception as e:
        print(f"[彩虹表] 构建失败：{e}")
        # 清理半成品正式表（.partial 留在原地不碍事，下次构建会覆盖，这里一并清掉）
        for p in (table_path, table_path + ".partial"):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        return False
    finally:
        # 清理临时分片文件
        for p in tmp_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def _get_mmap(table_path: str):
    """获取表的 mmap 只读映射（进程级缓存）。表不存在或为空返回 None。"""
    if table_path in _mmap_cache:
        return _mmap_cache[table_path][1]
    try:
        f = open(table_path, "rb")
        if os.fstat(f.fileno()).st_size == 0:
            f.close()
            return None
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    except (OSError, ValueError):
        return None
    _mmap_cache[table_path] = (f, mm)
    return mm


def _read_crc(mm, idx: int) -> int:
    """读第 idx 条记录的 crc32"""
    return _RECORD.unpack_from(mm, idx * RECORD_SIZE)[0]


def table_exists() -> bool:
    """表文件是否存在且大小合理（非空、为 8 的倍数）"""
    table_path = _get_table_path()
    try:
        size = os.path.getsize(table_path)
    except OSError:
        return False
    return size > 0 and size % RECORD_SIZE == 0


def lookup(crc32_hash: str) -> list[int]:
    """
    查表：输入 8 位 hex 字符串（如 '4200b4cd'），返回所有匹配 uid（升序）。
    表不存在或损坏、输入非法时返回空列表。
    """
    try:
        target = int(crc32_hash.strip(), 16)
    except (ValueError, AttributeError):
        return []
    if not (0 <= target <= 0xFFFFFFFF):
        return []
    if not table_exists():
        return []

    mm = _get_mmap(_get_table_path())
    if mm is None:
        return []
    try:
        n = len(mm) // RECORD_SIZE
        # 手写二分：定位第一条 crc32 >= target 的记录（约 log2(n) ≈ 26 次读取）
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if _read_crc(mm, mid) < target:
                lo = mid + 1
            else:
                hi = mid
        # 从 lo 向后扫描收集全部同 crc32 的 uid（碰撞记录相邻）
        results = []
        i = lo
        while i < n and _read_crc(mm, i) == target:
            results.append(_RECORD.unpack_from(mm, i * RECORD_SIZE)[1])
            i += 1
        results.sort()
        return results
    except (OSError, ValueError, struct.error):
        # mmap 读取异常（如表被外部截断）按表损坏处理
        return []

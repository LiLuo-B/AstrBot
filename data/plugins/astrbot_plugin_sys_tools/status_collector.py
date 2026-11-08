"""Collect PicStatus-compatible metrics without importing the bot framework."""

from __future__ import annotations

import asyncio
import csv
import math
import platform
import re
import subprocess
import time
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import psutil

__all__ = ["check_connectivity", "collect_status", "format_status_text"]

_READ_ERRORS = (OSError, ValueError, RuntimeError, psutil.Error)
_GPU_COMMAND = (
    "nvidia-smi",
    "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
    "temperature.gpu,clocks.gr,clocks.mem",
    "--format=csv,noheader,nounits",
)


def _collect_gpu(warnings: list[str]) -> list[dict]:
    """Read NVIDIA metrics, retaining other GPUs when one field is unavailable.

    Args:
        warnings: Destination for diagnostic messages about unavailable metrics.

    Returns:
        GPU dictionaries with byte-sized memory and MHz clock values.
    """
    try:
        completed = subprocess.run(
            _GPU_COMMAND,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4.0,
            check=True,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        warnings.append(f"GPU collection failed: {type(exc).__name__}: {exc}")
        return []

    fields = (
        "utilization",
        "memory_used",
        "memory_total",
        "temperature",
        "clock_graphics",
        "clock_memory",
    )
    gpus = []
    for index, line in enumerate(completed.stdout.splitlines()):
        if not line.strip():
            continue
        try:
            row = next(csv.reader([line], skipinitialspace=True, strict=True))
        except csv.Error as exc:
            warnings.append(f"GPU row {index} has invalid CSV: {exc}")
            continue
        if len(row) < 2:
            warnings.append(f"GPU row {index} has no metric columns")
            continue
        if len(row) != 7:
            warnings.append(f"GPU row {index} has {len(row)} columns; expected 7")
        row = (row + [""] * 7)[:7]
        gpu = {"name": row[0].strip() or f"GPU {index}"}
        for field, raw in zip(fields, row[1:]):
            raw = raw.strip()
            value = None
            if raw.lower().strip("[]") not in {
                "",
                "n/a",
                "not supported",
                "unknown",
                "-",
            }:
                try:
                    number = float(raw)
                    if not math.isfinite(number) or number < 0:
                        raise ValueError("expected a finite nonnegative value")
                    if field == "utilization" and number > 100:
                        raise ValueError("utilization exceeds 100 percent")
                    if field.startswith("memory_"):
                        value = int(number * 1024 * 1024)
                        if field == "memory_total" and value == 0:
                            value = None
                    else:
                        value = number
                except (ValueError, OverflowError) as exc:
                    warnings.append(f"GPU {index} {field} is invalid: {raw!r}: {exc}")
            gpu[field] = value
        gpus.append(gpu)
    return gpus


def _disk_names(
    counters: dict, configured: list[str] | None, warnings: list[str]
) -> list[str]:
    """Select whole block devices rather than their partitions or loop devices.

    Args:
        counters: Current per-device disk counters.
        configured: Exact administrator-selected names, or automatic selection.
        warnings: Destination for diagnostics about unavailable devices.

    Returns:
        Available device names, preserving explicit configuration order.
    """
    if configured is not None:
        names = list(dict.fromkeys(configured))
        for name in names:
            if name not in counters:
                warnings.append(f"Configured disk is unavailable: {name}")
        return [name for name in names if name in counters]

    top_level = None
    if platform.system() == "Linux":
        try:
            top_level = {entry.name for entry in Path("/sys/block").iterdir()}
        except OSError as exc:
            warnings.append(f"Block device discovery failed; using names: {exc}")
    names = []
    for name in counters:
        # Device-mapper layers would duplicate the underlying physical device.
        if re.match(r"^(?:loop|zram|ram|fd|sr|dm-)", name):
            continue
        if top_level is not None:
            if name in top_level:
                names.append(name)
        elif re.fullmatch(
            r"(?:sd|hd|vd|xvd)[a-z]+|nvme\d+n\d+|mmcblk\d+|md\d+|"
            r"disk\d+|PhysicalDrive\d+|\d+(?: [A-Z]:)*",
            name,
            flags=re.IGNORECASE,
        ):
            names.append(name)
    return names


def _network_names(counters: dict, configured: list[str] | None) -> list[str]:
    """Keep physical NICs and bridges without letting tunnel/veth traffic dominate.

    Args:
        counters: Current per-interface network counters.
        configured: Exact administrator-selected names, overriding auto-filtering.

    Returns:
        Interface names, including idle physical NICs and configured virtual NICs.
    """
    if configured is not None:
        return [name for name in dict.fromkeys(configured) if name in counters]
    names = []
    for name in counters:
        if re.match(
            r"^(?:lo(?:op)?\d*$|loopback|本地连接|veth|[ut]tun|tun|tap|"
            r"wg\d|tailscale|zerotier|zt[0-9a-f]|ip6tnl|sit\d|gre\d|"
            r"gretap|erspan|ifb\d|dummy\d|nflog|nfqueue)",
            name,
            flags=re.IGNORECASE,
        ):
            continue
        if platform.system() == "Linux":
            physical = (Path("/sys/class/net") / name / "device").exists()
            bridge = (Path("/sys/class/net") / name / "bridge").exists()
            if not (
                physical
                or bridge
                or re.match(
                    r"^(?:eth|en|wl|wlan|wwan|ib|em|p\d+p\d+|docker|br|bond)", name
                )
            ):
                continue
        names.append(name)
    return names


def collect_status(
    top_n: int = 10,
    show_gpu: bool = True,
    process_sort_by: str = "mem",
    network_interfaces: list[str] | None = None,
    disk_devices: list[str] | None = None,
    sample_seconds: float = 0.5,
    display_options: dict | None = None,
) -> dict:
    r"""Collect synchronous system metrics across a single shared sampling interval.

    Memory usage is total minus available, consistent with the displayed percent.
    Process CPU uses psutil's per-core convention and can exceed 100 percent,
    unless proc_cpu_max_100p normalizes it by the logical CPU count.
    Processes remain separate even when their complete names are identical.
    Missing metrics are None, not fabricated zero readings; diagnostics are in
    warnings. Device counter resets are clamped to zero for the affected field.

    Args:
        top_n: Maximum process rows; zero disables process collection.
        show_gpu: Whether to run the fixed, four-second-bounded nvidia-smi query.
        process_sort_by: Process ordering, either "mem" (RSS bytes) or "cpu".
        network_interfaces: Exact NIC allowlist; an empty list disables network
            rows. None discovers physical NICs and bridges in legacy mode, or all
            sampled interfaces when display_options is provided.
        disk_devices: Exact disk allowlist, e.g. nvme0n1, nvme1n1, md0. An empty
            list disables disk I/O rows. None selects whole block devices in
            legacy mode, or all sampled devices when display_options is provided.
            Explicit lists bypass legacy discovery but still honor display options.
        sample_seconds: Shared wait, clamped to 0.05 through 2 seconds. Throughput
            uses the measured monotonic duration rather than this requested wait.
        display_options: None preserves legacy automatic device filtering and
            sorting. A dictionary opts into upstream PicStatus defaults: ignore_parts
            [], ignore_bad_parts False, sort_parts True, sort_parts_reverse False,
            ignore_disk_ios [r"^(loop|zram)\d*$"], ignore_no_io_disk False,
            sort_disk_ios True, ignore_nets [r"^lo(op)?\d*$|^(Loopback|本地连接)"],
            ignore_0b_net False, sort_nets True, ignore_procs
            ["^System Idle Process$"], and proc_cpu_max_100p False. Ignore lists
            use case-sensitive regex searches against mountpoints or full names.
            Sorting overrides explicit allowlist order; disabled sorting preserves
            discovery or allowlist order. No disk or network row limit is applied.

    Returns:
        Renderer data with aware local time, byte-sized memory/RSS, byte/second
        throughput, GPU clocks in MHz, and an initially empty network_connection.
        Extra fields warnings and sample_duration explain degraded collection.

    Raises:
        ValueError: If the sort key, process count, sample interval, or ignore
            regex is invalid. Regex validation happens before any sampling.
    """
    if not isinstance(top_n, int) or top_n < 0:
        raise ValueError("top_n must be a nonnegative integer")
    if process_sort_by not in {"mem", "cpu"}:
        raise ValueError("process_sort_by must be 'mem' or 'cpu'")
    if not math.isfinite(sample_seconds) or sample_seconds < 0:
        raise ValueError("sample_seconds must be finite and nonnegative")
    options = {
        "ignore_parts": [],
        "ignore_bad_parts": False,
        "sort_parts": True,
        "sort_parts_reverse": False,
        "ignore_disk_ios": [r"^(loop|zram)\d*$"],
        "ignore_no_io_disk": False,
        "sort_disk_ios": True,
        "ignore_nets": [r"^lo(op)?\d*$|^(Loopback|本地连接)"],
        "ignore_0b_net": False,
        "sort_nets": True,
        "ignore_procs": ["^System Idle Process$"],
        "proc_cpu_max_100p": False,
    }
    patterns = {}
    if display_options is not None:
        options.update(display_options)
        for key in ("ignore_parts", "ignore_disk_ios", "ignore_nets", "ignore_procs"):
            try:
                patterns[key] = [re.compile(pattern) for pattern in options[key]]
            except (re.error, TypeError, OverflowError) as exc:
                raise ValueError(
                    f"Invalid regex in display_options.{key}: {exc}"
                ) from exc
    sample_wait = min(2.0, max(0.05, sample_seconds))
    warnings: list[str] = []
    if sample_wait != sample_seconds:
        warnings.append(f"Sampling wait clamped to {sample_wait:g} seconds")

    # Fresh Process objects prevent process_iter's cached CPU baselines from
    # leaking across simultaneous collectors. Warm each one before the same wait.
    processes = []
    if top_n:
        try:
            for item in psutil.process_iter():
                try:
                    proc = psutil.Process(item.pid)
                    proc.cpu_percent(interval=None)
                    processes.append(proc)
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    continue
                except _READ_ERRORS as exc:
                    warnings.append(f"Process {item.pid} warmup unavailable: {exc}")
        except _READ_ERRORS as exc:
            warnings.append(f"Process discovery unavailable: {exc}")

    cpu_ready = True
    try:
        psutil.cpu_percent(interval=None)
    except _READ_ERRORS as exc:
        cpu_ready = False
        warnings.append(f"CPU sampling warmup unavailable: {exc}")
    sample_started = time.monotonic()
    try:
        disk_before = psutil.disk_io_counters(perdisk=True, nowrap=False) or {}
    except _READ_ERRORS as exc:
        disk_before = {}
        warnings.append(f"Disk I/O baseline unavailable: {exc}")
    try:
        net_before = psutil.net_io_counters(pernic=True, nowrap=False) or {}
    except _READ_ERRORS as exc:
        net_before = {}
        warnings.append(f"Network I/O baseline unavailable: {exc}")

    time.sleep(sample_wait)

    try:
        disk_after = psutil.disk_io_counters(perdisk=True, nowrap=False) or {}
    except _READ_ERRORS as exc:
        disk_after = {}
        warnings.append(f"Disk I/O sample unavailable: {exc}")
    try:
        net_after = psutil.net_io_counters(pernic=True, nowrap=False) or {}
    except _READ_ERRORS as exc:
        net_after = {}
        warnings.append(f"Network I/O sample unavailable: {exc}")
    elapsed = max(time.monotonic() - sample_started, 1e-9)
    cpu_percent = None
    if cpu_ready:
        try:
            cpu_percent = max(0.0, min(100.0, float(psutil.cpu_percent(interval=None))))
        except _READ_ERRORS as exc:
            warnings.append(f"CPU sample unavailable: {exc}")

    cpu_count = 0
    cpu_count_logical = 0
    for logical in (False, True):
        try:
            count = psutil.cpu_count(logical=logical)
            if count is None:
                warnings.append(f"CPU count unavailable (logical={logical})")
            if logical:
                cpu_count_logical = int(count or 0)
            else:
                cpu_count = int(count or 0)
        except _READ_ERRORS as exc:
            warnings.append(f"CPU count unavailable (logical={logical}): {exc}")

    process_status = []
    for proc in processes:
        try:
            # is_running checks creation time as well as PID, avoiding PID reuse.
            if not proc.is_running():
                continue
            with proc.oneshot():
                name = proc.name()
                if display_options is not None and any(
                    pattern.search(name) for pattern in patterns["ignore_procs"]
                ):
                    continue
                cpu = max(0.0, float(proc.cpu_percent(interval=None)))
                if (
                    display_options is not None
                    and options["proc_cpu_max_100p"]
                    and cpu_count_logical
                ):
                    # Upstream normalizes by logical CPUs rather than clamping.
                    cpu /= cpu_count_logical
                process_status.append(
                    {
                        "pid": proc.pid,
                        "name": name,
                        "cpu": cpu,
                        "mem": int(proc.memory_info().rss),
                    }
                )
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except _READ_ERRORS as exc:
            warnings.append(f"Process {proc.pid} sample unavailable: {exc}")
    process_status.sort(
        key=lambda proc: (-proc[process_sort_by], -proc["mem"], proc["pid"])
    )

    disk_io = []
    if display_options is None or disk_devices is not None:
        disk_names = _disk_names(disk_after, disk_devices, warnings)
    else:
        disk_names = list(disk_before)
    for name in disk_names:
        if name not in disk_after:
            continue
        if display_options is not None and any(
            pattern.search(name) for pattern in patterns["ignore_disk_ios"]
        ):
            continue
        if name not in disk_before:
            warnings.append(f"Disk {name} has no sampling baseline")
            continue
        before, after = disk_before[name], disk_after[name]
        read = max(0, after.read_bytes - before.read_bytes) / elapsed
        write = max(0, after.write_bytes - before.write_bytes) / elapsed
        if (
            display_options is not None
            and options["ignore_no_io_disk"]
            and read == 0
            and write == 0
        ):
            continue
        disk_io.append({"name": name, "read": read, "write": write})
    if (
        options["sort_disk_ios"]
        if display_options is not None
        else disk_devices is None
    ):
        disk_io.sort(key=lambda item: item["read"] + item["write"], reverse=True)

    network_io = []
    if network_interfaces is not None:
        for name in dict.fromkeys(network_interfaces):
            if name not in net_after:
                warnings.append(f"Configured network interface is unavailable: {name}")
    if display_options is None or network_interfaces is not None:
        network_names = _network_names(net_after, network_interfaces)
    else:
        network_names = list(net_before)
    for name in network_names:
        if name not in net_after:
            continue
        if display_options is not None and any(
            pattern.search(name) for pattern in patterns["ignore_nets"]
        ):
            continue
        if name not in net_before:
            warnings.append(f"Network interface {name} has no sampling baseline")
            continue
        before, after = net_before[name], net_after[name]
        sent = max(0, after.bytes_sent - before.bytes_sent) / elapsed
        recv = max(0, after.bytes_recv - before.bytes_recv) / elapsed
        if (
            display_options is not None
            and options["ignore_0b_net"]
            and sent == 0
            and recv == 0
        ):
            continue
        network_io.append({"name": name, "sent": sent, "recv": recv})
    if display_options is not None:
        if options["sort_nets"]:
            network_io.sort(key=lambda item: item["sent"] + item["recv"], reverse=True)
    elif network_interfaces is None:
        # Physical ports stay before bridges even when an idle Wi-Fi NIC is down.
        network_io.sort(
            key=lambda item: (
                1
                if item["name"] == "docker0"
                else (2 if re.match(r"^(?:docker|br|virbr|vnet)", item["name"]) else 0),
                -(item["sent"] + item["recv"]),
            )
        )

    cpu_brand = platform.processor() or platform.machine() or "Unknown CPU"
    if platform.system() == "Linux":
        try:
            cpuinfo = {}
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition(":")
                if separator and value.strip():
                    cpuinfo.setdefault(key.strip().lower(), value.strip())
            for key in ("model name", "hardware", "cpu model", "processor"):
                candidate = cpuinfo.get(key, "")
                if candidate and not candidate.isdecimal():
                    cpu_brand = candidate
                    break
        except (OSError, UnicodeError) as exc:
            warnings.append(f"CPU model unavailable; using platform fallback: {exc}")
    cpu_freq = None
    try:
        freq = psutil.cpu_freq()
        if freq is not None:
            cpu_freq = {
                key: value if math.isfinite(value) and value > 0 else None
                for key in ("current", "max")
                for value in (float(getattr(freq, key, 0) or 0),)
            }
    except _READ_ERRORS as exc:
        warnings.append(f"CPU frequency unavailable: {exc}")

    memory_stat = {"used": None, "total": None, "percent": None}
    try:
        memory = psutil.virtual_memory()
        total = int(memory.total)
        used = max(0, min(total, total - int(memory.available)))
        memory_stat = {
            "used": used,
            "total": total,
            "percent": used / total * 100 if total else 0.0,
        }
    except _READ_ERRORS as exc:
        warnings.append(f"Memory statistics unavailable: {exc}")
    swap_stat = {"used": None, "total": None, "percent": None}
    try:
        swap = psutil.swap_memory()
        total, used = int(swap.total), int(swap.used)
        swap_stat = {
            "used": used,
            "total": total,
            "percent": used / total * 100 if total else 0.0,
        }
    except _READ_ERRORS as exc:
        warnings.append(f"Swap statistics unavailable: {exc}")
    load_avg = None
    try:
        load = psutil.getloadavg()
        load_avg = dict(zip(("load_1min", "load_5min", "load_15min"), map(float, load)))
    except _READ_ERRORS as exc:
        warnings.append(f"Load averages unavailable: {exc}")

    mountpoints = []
    try:
        for partition in psutil.disk_partitions(all=False):
            mountpoint = partition.mountpoint
            if display_options is None:
                if partition.fstype == "squashfs" or mountpoint.startswith(
                    ("/snap/", "/var/lib/snapd/snap/")
                ):
                    continue
            elif any(
                pattern.search(mountpoint) for pattern in patterns["ignore_parts"]
            ):
                continue
            if mountpoint not in mountpoints:
                mountpoints.append(mountpoint)
    except _READ_ERRORS as exc:
        warnings.append(f"Disk partition discovery unavailable: {exc}")
    if not mountpoints and display_options is None:
        mountpoints = [Path.cwd().anchor or "/"]
    disk_usage = []
    for mountpoint in mountpoints:
        entry = {
            "name": mountpoint,
            "percent": None,
            "used": None,
            "total": None,
            "exception": None,
        }
        try:
            usage = psutil.disk_usage(mountpoint)
            entry.update(percent=usage.percent, used=usage.used, total=usage.total)
        except _READ_ERRORS as exc:
            entry["exception"] = f"{type(exc).__name__}: {exc}"
            warnings.append(f"Disk usage unavailable for {mountpoint}: {exc}")
            if display_options is not None and options["ignore_bad_parts"]:
                continue
        disk_usage.append(entry)
    if display_options is None or options["sort_parts"]:
        disk_usage.sort(
            key=lambda entry: entry["percent"] if entry["percent"] is not None else -1,
            reverse=display_options is None or not options["sort_parts_reverse"],
        )

    system = f"{platform.system()} {platform.release()} {platform.machine()}".strip()
    if platform.system() == "Linux":
        try:
            distro = platform.freedesktop_os_release()
            pretty = distro.get("PRETTY_NAME") or distro.get("NAME")
            if pretty:
                system = f"{pretty} ({platform.release()}, {platform.machine()})"
        except OSError as exc:
            warnings.append(f"Linux distribution unavailable; using platform: {exc}")
    gpu_stat = _collect_gpu(warnings) if show_gpu else []
    system_run_time = None
    try:
        system_run_time = max(0.0, time.time() - psutil.boot_time())
    except _READ_ERRORS as exc:
        warnings.append(f"System uptime unavailable: {exc}")
    return {
        "hostname": platform.node(),
        "system": system,
        "python_version": f"{platform.python_implementation()} {platform.python_version()}",
        "time": datetime.now().astimezone(),
        "system_run_time": system_run_time,
        "cpu_percent": cpu_percent,
        "cpu_count": cpu_count,
        "cpu_count_logical": cpu_count_logical,
        "cpu_brand": cpu_brand,
        "cpu_freq": cpu_freq,
        "memory_stat": memory_stat,
        "swap_stat": swap_stat,
        "load_avg": load_avg,
        "gpu_stat": gpu_stat,
        "disk_usage": disk_usage,
        "disk_io": disk_io,
        "network_io": network_io,
        "network_connection": [],
        "process_status": process_status[:top_n],
        "warnings": warnings,
        "sample_duration": elapsed,
    }


async def check_connectivity(
    sites: list[dict], timeout: float = 5.0, proxy: str | None = None
) -> list[dict]:
    """Check trusted administrator-configured sites without downloading bodies.

    This is not an arbitrary user-target probing API. The caller must only pass
    fixed sites from administrator configuration, never chat-supplied addresses.
    Redirects are followed, environment proxies are ignored, and the deadline
    covers the entire redirect/header request, not each individual read. At most
    eight requests are in flight. Results preserve configuration order.

    Proxy handling mirrors upstream PicStatus: one global proxy address, plus a
    per-site ``use_proxy`` flag. Sites without the flag connect directly, so a
    broken proxy can never hide local connectivity.

    Args:
        sites: Trusted configuration entries with name, HTTP(S) url, and an
            optional use_proxy boolean.
        timeout: Positive, finite overall request timeout in seconds, per site.
        proxy: Global proxy URL applied only to sites with use_proxy set.

    Returns:
        Dictionaries with name, status, reason, delay in milliseconds, and error.
        Failures retain actual elapsed time and have status None, never a fake 200.

    Raises:
        ValueError: If timeout is nonpositive or nonfinite.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if not sites:
        return []
    semaphore = asyncio.Semaphore(8)
    async with AsyncExitStack() as stack:
        options = {
            "trust_env": False,
            "follow_redirects": True,
            "timeout": httpx.Timeout(timeout),
            "limits": httpx.Limits(max_connections=8, max_keepalive_connections=8),
        }
        direct_client = await stack.enter_async_context(httpx.AsyncClient(**options))
        needs_proxy = any(site.get("use_proxy", False) for site in sites)
        proxy_client = None
        proxy_error = None
        if needs_proxy:
            if proxy:
                try:
                    proxy_client = await stack.enter_async_context(
                        httpx.AsyncClient(proxy=proxy, **options)
                    )
                except (ValueError, httpx.InvalidURL) as exc:
                    proxy_error = f"Invalid proxy configuration: {exc}"
            else:
                proxy_error = "Site requires a proxy but no proxy is configured"

        async def read_headers(
            client: httpx.AsyncClient, url: httpx.URL, start: float
        ) -> dict:
            """Read and close headers within the caller's overall deadline.

            Args:
                client: Direct or explicitly proxied client for this site.
                url: Validated configured destination.
                start: Monotonic performance timestamp before the request.

            Returns:
                Actual response status, reason, elapsed milliseconds, and no error.
            """
            request = client.build_request("GET", url)
            # HTTPX's automatic redirect loop reads intermediate bodies even
            # with stream=True. Follow next_request explicitly and close each
            # response so neither redirect nor final bodies are downloaded.
            for _ in range(client.max_redirects + 1):
                response = await client.send(
                    request, stream=True, follow_redirects=False
                )
                try:
                    if response.next_request is None:
                        return {
                            "status": response.status_code,
                            "reason": response.reason_phrase,
                            "delay": (time.perf_counter() - start) * 1000,
                            "error": None,
                        }
                    request = response.next_request
                finally:
                    await response.aclose()
            raise httpx.TooManyRedirects(
                "Exceeded configured redirect limit", request=request
            )

        async def check_site(site: dict) -> dict:
            """Limit one configured probe and label request errors truthfully.

            Args:
                site: A trusted administrator configuration entry.

            Returns:
                A named success or failure result with real elapsed time.
            """
            async with semaphore:
                start = time.perf_counter()
                result = {
                    "name": str(site.get("name") or site.get("url") or "Unnamed site"),
                    "status": None,
                    "reason": None,
                    "delay": None,
                    "error": None,
                }
                try:
                    url = httpx.URL(str(site.get("url") or ""))
                    if url.scheme not in {"http", "https"} or not url.host:
                        raise ValueError(
                            "Configured site requires an absolute HTTP(S) URL"
                        )
                    # A site either uses the global proxy or connects directly.
                    if site.get("use_proxy", False):
                        if proxy_client is None:
                            raise ValueError(
                                proxy_error or "Proxy is unavailable for this site"
                            )
                        client = proxy_client
                    else:
                        client = direct_client
                    result.update(
                        await asyncio.wait_for(
                            read_headers(client, url, start), timeout=timeout
                        )
                    )
                except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
                    label = type(exc).__name__
                    result.update(
                        reason=label,
                        error=f"{label}: request exceeded a timeout ({timeout:g}s maximum)",
                        delay=(time.perf_counter() - start) * 1000,
                    )
                except (httpx.HTTPError, httpx.InvalidURL, ValueError, OSError) as exc:
                    result.update(
                        reason=type(exc).__name__,
                        error=f"{type(exc).__name__}: {exc}",
                        delay=(time.perf_counter() - start) * 1000,
                    )
                return result

        return await asyncio.gather(*(check_site(site) for site in sites))


def _format_bytes(value: float | None) -> str:
    """Format a byte-sized value without pretending an unavailable metric is zero.

    Args:
        value: Bytes, or None for an unavailable metric.

    Returns:
        Human-readable IEC units, or N/A.
    """
    if value is None:
        return "N/A"
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PiB"


def format_status_text(data: dict) -> str:
    """Format collected data for a text-only fallback without additional I/O.

    Args:
        data: A dictionary returned by collect_status, optionally with connectivity.

    Returns:
        Multiline status text preserving complete process names and PID identity.
    """
    stamp = data.get("time")
    if isinstance(stamp, datetime):
        stamp = stamp.strftime("%Y-%m-%d %H:%M:%S %z")
    uptime = data.get("system_run_time")
    uptime = str(timedelta(seconds=int(uptime))) if uptime is not None else "N/A"
    cpu = data.get("cpu_percent")
    cpu = f"{cpu:.1f}%" if cpu is not None else "N/A"
    lines = [
        f"{data.get('hostname', 'N/A')} | {data.get('system', 'N/A')}",
        f"{stamp or 'N/A'} | {data.get('python_version', 'N/A')}",
        f"运行时间: {uptime}",
        f"CPU: {data.get('cpu_brand', 'N/A')} | {cpu} | "
        f"{data.get('cpu_count', 0)} 核 / {data.get('cpu_count_logical', 0)} 线程",
    ]
    for key, title in (("memory_stat", "内存"), ("swap_stat", "交换")):
        stat = data.get(key) or {}
        percent = stat.get("percent")
        percent = f"{percent:.1f}%" if percent is not None else "N/A"
        lines.append(
            f"{title}: {_format_bytes(stat.get('used'))} / "
            f"{_format_bytes(stat.get('total'))} ({percent})"
        )
    load = data.get("load_avg")
    if load:
        lines.append("负载: " + " / ".join(f"{value:.2f}" for value in load.values()))
    for gpu in data.get("gpu_stat", []):
        utilization = gpu.get("utilization")
        utilization = f"{utilization:.1f}%" if utilization is not None else "N/A"
        temperature = gpu.get("temperature")
        temperature = f"{temperature:g}°C" if temperature is not None else "N/A"
        lines.append(
            f"GPU {gpu['name']}: {utilization}, "
            f"{_format_bytes(gpu.get('memory_used'))} / "
            f"{_format_bytes(gpu.get('memory_total'))}, {temperature}"
        )
    for disk in data.get("disk_usage", []):
        if disk.get("exception"):
            lines.append(f"磁盘 {disk['name']}: {disk['exception']}")
        else:
            lines.append(
                f"磁盘 {disk['name']}: {_format_bytes(disk.get('used'))} / "
                f"{_format_bytes(disk.get('total'))} ({disk['percent']:.1f}%)"
            )
    for disk in data.get("disk_io", []):
        lines.append(
            f"I/O {disk['name']}: 读 {_format_bytes(disk['read'])}/s | "
            f"写 {_format_bytes(disk['write'])}/s"
        )
    for net in data.get("network_io", []):
        lines.append(
            f"网络 {net['name']}: ↑ {_format_bytes(net['sent'])}/s | "
            f"↓ {_format_bytes(net['recv'])}/s"
        )
    for site in data.get("network_connection", []):
        delay = site.get("delay")
        delay = f"{delay:.2f} ms" if delay is not None else "N/A"
        detail = site.get("error") or f"{site.get('status')} {site.get('reason', '')}"
        lines.append(f"连通性 {site['name']}: {detail} | {delay}")
    if data.get("process_status"):
        lines.append("进程 (PID | 名称 | CPU | RSS):")
        for proc in data["process_status"]:
            lines.append(
                f"{proc['pid']} | {proc['name']} | {proc['cpu']:.1f}% | "
                f"{_format_bytes(proc['mem'])}"
            )
    if data.get("warnings"):
        lines.append("Warnings: " + "; ".join(data["warnings"]))
    return "\n".join(lines)

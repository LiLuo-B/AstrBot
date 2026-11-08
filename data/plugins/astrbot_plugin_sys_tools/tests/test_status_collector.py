"""Standalone regression tests; HTTP is mocked and AstrBot is never imported."""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "status_collector.py"
SPEC = importlib.util.spec_from_file_location(
    "standalone_status_collector", MODULE_PATH
)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class FakeProcess:
    """Track an individual PID's independent CPU warmup and RSS readings."""

    def __init__(self, pid, name="python", mem=100, cpu=5.0, disappears=False):
        """Initialize one simulated process.

        Args:
            pid: Distinct process identifier.
            name: Complete process name.
            mem: RSS bytes.
            cpu: Post-warmup per-core CPU percentage.
            disappears: Whether the process exits before its final name read.
        """
        self.pid = pid
        self.full_name = name
        self.mem = mem
        self.cpu = cpu
        self.calls = 0
        self.disappears = disappears

    def cpu_percent(self, interval=None):
        """Return zero until CPU has been primed.

        Args:
            interval: Unused nonblocking psutil-compatible argument.

        Returns:
            The post-warmup CPU value, or zero on the first read.
        """
        assert interval is None
        self.calls += 1
        return self.cpu if self.calls > 1 else 0.0

    def is_running(self):
        """Report identity unchanged.

        Returns:
            True for an unrecycled PID.
        """
        return True

    def oneshot(self):
        """Provide psutil's context-manager shape.

        Returns:
            A no-op context manager.
        """
        return nullcontext()

    def name(self):
        """Read the complete name, or simulate an independently exiting PID.

        Returns:
            The full process name.

        Raises:
            collector.psutil.NoSuchProcess: When configured to exit.
        """
        if self.disappears:
            raise collector.psutil.NoSuchProcess(self.pid)
        return self.full_name

    def memory_info(self):
        """Read resident memory.

        Returns:
            A psutil-compatible RSS object.
        """
        return SimpleNamespace(rss=self.mem)


@pytest.fixture
def metrics(monkeypatch):
    """Install deterministic local counters with no waits or subprocesses.

    Args:
        monkeypatch: Pytest's isolated patch manager.

    Returns:
        Captured calls, process instances, and a fake measured clock.
    """
    state = SimpleNamespace(
        now=100.0, sleeps=[], disk_calls=0, net_calls=0, cpu_calls=0
    )

    def sleep(seconds):
        """Advance twice the requested wait to expose requested-time rate bugs.

        Args:
            seconds: Requested sample wait.
        """
        state.sleeps.append(seconds)
        state.now += seconds * 2

    monkeypatch.setattr(
        collector,
        "time",
        SimpleNamespace(
            monotonic=lambda: state.now,
            sleep=sleep,
            time=lambda: 2000.0,
        ),
    )
    state.processes = {
        1: FakeProcess(1, "python", mem=100, cpu=250.0),
        2: FakeProcess(2, "python", mem=300, cpu=20.0),
        3: FakeProcess(3, "a-very-long-untruncated-process-name", mem=200, cpu=10.0),
        4: FakeProcess(4, mem=9000, disappears=True),
    }
    monkeypatch.setattr(
        collector.psutil, "process_iter", lambda: iter(state.processes.values())
    )
    monkeypatch.setattr(collector.psutil, "Process", state.processes.__getitem__)

    def cpu_percent(interval=None):
        """Require a nonblocking CPU warmup.

        Args:
            interval: Expected nonblocking argument.

        Returns:
            Zero first, then the real sample.
        """
        assert interval is None
        state.cpu_calls += 1
        return 12.5 if state.cpu_calls > 1 else 0.0

    monkeypatch.setattr(collector.psutil, "cpu_percent", cpu_percent)
    monkeypatch.setattr(
        collector.psutil, "cpu_count", lambda logical=True: 8 if logical else 4
    )
    monkeypatch.setattr(
        collector.psutil,
        "cpu_freq",
        lambda: SimpleNamespace(current=2000.0, max=4000.0),
    )
    monkeypatch.setattr(
        collector.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=1000, available=600, used=123, percent=40),
    )
    monkeypatch.setattr(
        collector.psutil,
        "swap_memory",
        lambda: SimpleNamespace(total=0, used=0, percent=0.0),
    )
    monkeypatch.setattr(collector.psutil, "getloadavg", lambda: (1.0, 2.0, 3.0))
    monkeypatch.setattr(collector.psutil, "boot_time", lambda: 1000.0)
    monkeypatch.setattr(
        collector.psutil,
        "disk_partitions",
        lambda all=False: [
            SimpleNamespace(mountpoint="/", fstype="ext4"),
            SimpleNamespace(mountpoint="/denied", fstype="ext4"),
            SimpleNamespace(mountpoint="/", fstype="ext4"),
            SimpleNamespace(mountpoint="/snap/test/1", fstype="squashfs"),
        ],
    )

    def disk_usage(mountpoint):
        """Deny one mount without losing other usage rows.

        Args:
            mountpoint: Filesystem mount to read.

        Returns:
            A psutil-compatible disk-usage object.

        Raises:
            PermissionError: For the configured inaccessible mount.
        """
        if mountpoint == "/denied":
            raise PermissionError("mount access denied")
        return SimpleNamespace(used=250, total=1000, percent=25.0)

    monkeypatch.setattr(collector.psutil, "disk_usage", disk_usage)

    def disk_io_counters(perdisk=False, nowrap=True):
        """Provide disk and partition counters, including reset fields.

        Args:
            perdisk: Whether per-device counters were requested.
            nowrap: Whether psutil's global wrap compensation is enabled.

        Returns:
            Device counter snapshots.
        """
        assert perdisk and nowrap is False
        state.disk_calls += 1
        second = state.disk_calls % 2 == 0
        return {
            name: SimpleNamespace(
                read_bytes=1100 if second else 100,
                write_bytes=0 if second else 500,
            )
            for name in ("nvme0n1", "nvme0n1p1", "md0", "loop0", "zram0", "dm-0")
        }

    monkeypatch.setattr(collector.psutil, "disk_io_counters", disk_io_counters)

    def net_io_counters(pernic=False, nowrap=True):
        """Provide idle hardware, active bridge, and excluded virtual NICs.

        Args:
            pernic: Whether per-interface counters were requested.
            nowrap: Whether global wrap compensation is enabled.

        Returns:
            Network counter snapshots.
        """
        assert pernic and nowrap is False
        state.net_calls += 1
        second = state.net_calls % 2 == 0
        return {
            name: SimpleNamespace(
                bytes_sent=100 if not second or name == "wlan0" else 120,
                bytes_recv=100 if not second or name == "wlan0" else 90,
            )
            for name in (
                "lo",
                "eth0",
                "wlan0",
                "docker0",
                "br-test",
                "veth123",
                "tun0",
                "utun7",
            )
        }

    monkeypatch.setattr(collector.psutil, "net_io_counters", net_io_counters)
    original_iterdir = Path.iterdir

    def iterdir(path):
        """Expose only top-level whole Linux block devices.

        Args:
            path: Directory being listed.

        Returns:
            Paths representing directory entries.
        """
        if path == Path("/sys/block"):
            return iter(
                Path("/sys/block") / name
                for name in ("nvme0n1", "md0", "loop0", "dm-0")
            )
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(collector.platform, "system", lambda: "Linux")
    return state


def test_shared_sampling_preserves_pid_and_units(metrics):
    """Verify one measured wait, per-PID process identity, and byte units.

    Args:
        metrics: Deterministic system metric fixture.
    """
    data = collector.collect_status(show_gpu=False)
    assert metrics.sleeps == [0.5]
    assert data["sample_duration"] == 1.0
    assert metrics.cpu_calls == 2
    assert data["cpu_percent"] == 12.5
    assert data["cpu_count"] == 4
    assert data["cpu_count_logical"] == 8
    assert data["time"].utcoffset() is not None
    assert data["system_run_time"] == 1000.0
    assert data["memory_stat"] == {"used": 400, "total": 1000, "percent": 40.0}
    assert data["swap_stat"] == {"used": 0, "total": 0, "percent": 0.0}
    assert [proc["pid"] for proc in data["process_status"]] == [2, 3, 1]
    assert [proc["name"] for proc in data["process_status"]].count("python") == 2
    assert data["process_status"][1]["name"] == "a-very-long-untruncated-process-name"
    assert data["process_status"][-1]["cpu"] == 250.0
    assert all(proc.calls == 2 for pid, proc in metrics.processes.items() if pid != 4)
    assert data["network_connection"] == []
    assert data["gpu_stat"] == []
    assert {disk["name"] for disk in data["disk_io"]} == {"nvme0n1", "md0"}
    assert all(
        disk["read"] == 1000.0 and disk["write"] == 0.0 for disk in data["disk_io"]
    )
    assert [net["name"] for net in data["network_io"]] == [
        "eth0",
        "wlan0",
        "docker0",
        "br-test",
    ]
    assert data["network_io"][1]["sent"] == 0.0
    assert data["network_io"][0]["sent"] == 20.0
    assert data["network_io"][0]["recv"] == 0.0
    assert len(data["disk_usage"]) == 2
    assert data["disk_usage"][0]["exception"] is None
    assert "PermissionError" in data["disk_usage"][1]["exception"]


def test_explicit_devices_and_cpu_order(metrics):
    """Honor exact lists, including explicit virtual devices, and CPU sorting.

    Args:
        metrics: Deterministic system metric fixture.
    """
    data = collector.collect_status(
        top_n=2,
        show_gpu=False,
        process_sort_by="cpu",
        network_interfaces=["tun0", "lo", "missing", "tun0"],
        disk_devices=["loop0", "nvme1n1", "md0", "loop0"],
    )
    assert [proc["pid"] for proc in data["process_status"]] == [1, 2]
    assert [net["name"] for net in data["network_io"]] == ["tun0", "lo"]
    assert [disk["name"] for disk in data["disk_io"]] == ["loop0", "md0"]
    assert any("nvme1n1" in warning for warning in data["warnings"])
    assert any("missing" in warning for warning in data["warnings"])


def test_zero_process_count_and_bounded_wait(metrics):
    """Disable process work and clamp an excessive sample wait.

    Args:
        metrics: Deterministic system metric fixture.
    """
    data = collector.collect_status(top_n=0, show_gpu=False, sample_seconds=999)
    assert metrics.sleeps == [2.0]
    assert data["process_status"] == []
    assert all(proc.calls == 0 for proc in metrics.processes.values())
    assert any("clamped" in warning for warning in data["warnings"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"process_sort_by": "rss"},
        {"top_n": -1},
        {"sample_seconds": -1},
        {"sample_seconds": float("nan")},
    ],
)
def test_invalid_arguments(kwargs):
    """Reject invalid arguments before sampling.

    Args:
        kwargs: Invalid collector arguments.
    """
    with pytest.raises(ValueError):
        collector.collect_status(**kwargs)


def test_unavailable_cpu_and_one_denied_pid(metrics, monkeypatch):
    """A single unavailable metric or process must not clear valid rows.

    Args:
        metrics: Deterministic system metric fixture.
        monkeypatch: Pytest's isolated patch manager.
    """
    original_factory = collector.psutil.Process

    def process(pid):
        """Deny one PID while retaining all other process objects.

        Args:
            pid: Requested process identifier.

        Returns:
            An accessible process.

        Raises:
            collector.psutil.AccessDenied: For the inaccessible process.
        """
        if pid == 1:
            raise collector.psutil.AccessDenied(pid)
        return original_factory(pid)

    def unavailable():
        """Simulate an unavailable optional metric.

        Raises:
            OSError: On every call.
        """
        raise OSError("metric unavailable")

    monkeypatch.setattr(collector.psutil, "Process", process)
    monkeypatch.setattr(collector.psutil, "cpu_freq", unavailable)
    monkeypatch.setattr(collector.psutil, "getloadavg", unavailable)
    data = collector.collect_status(show_gpu=False)
    assert [proc["pid"] for proc in data["process_status"]] == [2, 3]
    assert data["cpu_freq"] is None
    assert data["load_avg"] is None
    assert any("Process 1" in warning for warning in data["warnings"])


def test_new_and_removed_devices_do_not_discard_remaining_rows(metrics, monkeypatch):
    """Ignore devices without two samples without hiding stable devices.

    Args:
        metrics: Deterministic system metric fixture.
        monkeypatch: Pytest's isolated patch manager.
    """
    disk_reads = iter(
        [
            {"nvme0n1": SimpleNamespace(read_bytes=100, write_bytes=100)},
            {
                "nvme0n1": SimpleNamespace(read_bytes=200, write_bytes=200),
                "md0": SimpleNamespace(read_bytes=9000, write_bytes=9000),
            },
        ]
    )
    net_reads = iter(
        [
            {
                "eth0": SimpleNamespace(bytes_sent=100, bytes_recv=100),
                "wlan0": SimpleNamespace(bytes_sent=100, bytes_recv=100),
            },
            {
                "eth0": SimpleNamespace(bytes_sent=200, bytes_recv=200),
                "docker0": SimpleNamespace(bytes_sent=9000, bytes_recv=9000),
            },
        ]
    )
    monkeypatch.setattr(
        collector.psutil, "disk_io_counters", lambda **kwargs: next(disk_reads)
    )
    monkeypatch.setattr(
        collector.psutil, "net_io_counters", lambda **kwargs: next(net_reads)
    )
    data = collector.collect_status(show_gpu=False)
    assert [disk["name"] for disk in data["disk_io"]] == ["nvme0n1"]
    assert [net["name"] for net in data["network_io"]] == ["eth0"]
    assert any(
        "md0 has no sampling baseline" in warning for warning in data["warnings"]
    )
    assert any(
        "docker0 has no sampling baseline" in warning for warning in data["warnings"]
    )


def test_cpu_brand_procfs_and_platform_fallback(metrics, monkeypatch):
    """Prefer the full Linux CPU model and fall back without extra libraries.

    Args:
        metrics: Deterministic system metric fixture.
        monkeypatch: Pytest's isolated patch manager.
    """
    original_read = Path.read_text

    def read_cpuinfo(path, **kwargs):
        """Provide a deterministic Linux model string.

        Args:
            path: Requested text path.
            **kwargs: Text reading options.

        Returns:
            CPU metadata or ordinary file text.
        """
        if path == Path("/proc/cpuinfo"):
            return "processor\t: 0\nmodel name\t: Full Model With Long CPU Name\n"
        return original_read(path, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_cpuinfo)
    assert (
        collector.collect_status(show_gpu=False)["cpu_brand"]
        == "Full Model With Long CPU Name"
    )
    monkeypatch.setattr(collector.platform, "system", lambda: "OtherOS")
    monkeypatch.setattr(collector.platform, "processor", lambda: "Platform CPU")
    assert collector.collect_status(show_gpu=False)["cpu_brand"] == "Platform CPU"


def test_gpu_csv_partial_fields_and_bytes(monkeypatch):
    """Parse CSV names and N/A per-field while retaining other GPUs.

    Args:
        monkeypatch: Pytest's isolated patch manager.
    """
    calls = []

    def run(command, **kwargs):
        """Return representative nvidia-smi CSV without starting a process.

        Args:
            command: Fixed command tuple.
            **kwargs: Subprocess options.

        Returns:
            A completed-process shaped CSV response.
        """
        calls.append((command, kwargs))
        return SimpleNamespace(
            stdout='"GPU, One", 80, 1024, 2048, 65, 1500, 7000\nGPU Two, N/A, [Not Supported], 0, N/A, bad, 0\nGPU Three, 2, 1, 4\nbroken\n'
        )

    monkeypatch.setattr(collector.subprocess, "run", run)
    warnings = []
    data = collector._collect_gpu(warnings)
    assert len(data) == 3
    assert data[0] == {
        "name": "GPU, One",
        "utilization": 80.0,
        "memory_used": 1024 * 1024 * 1024,
        "memory_total": 2048 * 1024 * 1024,
        "temperature": 65.0,
        "clock_graphics": 1500.0,
        "clock_memory": 7000.0,
    }
    assert data[1]["memory_total"] is None
    assert data[1]["memory_used"] is None
    assert data[1]["clock_graphics"] is None
    assert data[1]["utilization"] is None
    assert data[2]["temperature"] is None
    assert data[2]["memory_used"] == 1024 * 1024
    assert warnings
    assert calls[0][0][0] == "nvidia-smi"
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] == 4.0


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("nvidia-smi"),
        subprocess.TimeoutExpired("nvidia-smi", 4),
        subprocess.CalledProcessError(1, "nvidia-smi"),
    ],
)
def test_gpu_subprocess_failures(monkeypatch, error):
    """Return no fabricated GPU metrics when the bounded command fails.

    Args:
        monkeypatch: Pytest's isolated patch manager.
        error: Expected subprocess failure to simulate.
    """

    def run(*args, **kwargs):
        """Simulate a subprocess failure.

        Args:
            *args: Ignored command arguments.
            **kwargs: Ignored command options.

        Raises:
            BaseException: The supplied subprocess failure.
        """
        raise error

    monkeypatch.setattr(collector.subprocess, "run", run)
    warnings = []
    assert collector._collect_gpu(warnings) == []
    assert type(error).__name__ in warnings[0]


def test_text_fallback_keeps_names_and_na(metrics):
    """Format errors and missing metrics without extra I/O.

    Args:
        metrics: Deterministic system metric fixture.
    """
    data = collector.collect_status(show_gpu=False)
    data["cpu_percent"] = None
    data["network_connection"] = [
        {
            "name": "offline",
            "status": None,
            "reason": "ConnectError",
            "delay": 12,
            "error": "ConnectError: offline",
        }
    ]
    text = collector.format_status_text(data)
    assert "a-very-long-untruncated-process-name" in text
    assert "ConnectError: offline | 12.00 ms" in text
    assert "N/A" in text
    assert "300.0 B" in text


def test_connectivity_redirect_status_stream_and_no_environment(monkeypatch):
    """Use truthful HTTP status and headers-only requests with trusted config.

    Args:
        monkeypatch: Pytest's isolated patch manager.
    """
    original_client = httpx.AsyncClient
    calls = []
    requested = []

    class UnreadBody(httpx.AsyncByteStream):
        """Fail immediately if connectivity collection downloads the body."""

        async def __aiter__(self):
            """Reject body reads.

            Raises:
                AssertionError: Whenever body iteration is requested.
            """
            raise AssertionError("Response bodies must not be downloaded")
            yield b""  # pragma: no cover

    def handler(request):
        """Supply a redirect followed by an actual forbidden status.

        Args:
            request: Mock transport request.

        Returns:
            A redirect or headers-only error response.
        """
        requested.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(
                302, headers={"Location": "/forbidden"}, stream=UnreadBody()
            )
        return httpx.Response(403, stream=UnreadBody())

    def client(**kwargs):
        """Inject a mock transport while preserving real HTTPX semantics.

        Args:
            **kwargs: Client creation options.

        Returns:
            An HTTPX client that cannot reach the network.
        """
        calls.append(kwargs)
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(collector.httpx, "AsyncClient", client)
    results = asyncio.run(
        collector.check_connectivity(
            [{"name": "configured", "url": "https://configured.invalid/redirect"}]
        )
    )
    assert [request.url.path for request in requested] == ["/redirect", "/forbidden"]
    assert results[0]["status"] == 403
    assert results[0]["reason"] == "Forbidden"
    assert results[0]["error"] is None
    assert results[0]["delay"] >= 0
    assert calls[0]["trust_env"] is False
    assert calls[0]["follow_redirects"] is True


def test_connectivity_parallel_timeout_errors_and_invalid_urls(monkeypatch):
    """Bound overall request time/concurrency and preserve distinct error labels.

    Args:
        monkeypatch: Pytest's isolated patch manager.
    """
    original_client = httpx.AsyncClient
    state = SimpleNamespace(active=0, maximum=0)

    async def handler(request):
        """Simulate simultaneous header requests with truthful failures.

        Args:
            request: Mock transport request.

        Returns:
            An actual 204 response for successful requests.

        Raises:
            httpx.ConnectError: For the configured connection error.
            httpx.ReadTimeout: For the configured read timeout.
        """
        state.active += 1
        state.maximum = max(state.maximum, state.active)
        try:
            if request.url.path == "/overall-timeout":
                await asyncio.sleep(1.0)
            if request.url.path == "/connect-error":
                raise httpx.ConnectError("connection refused", request=request)
            if request.url.path == "/read-timeout":
                raise httpx.ReadTimeout("read deadline exceeded", request=request)
            await asyncio.sleep(0.005)
            return httpx.Response(204)
        finally:
            state.active -= 1

    def client(**kwargs):
        """Inject mock-only networking.

        Args:
            **kwargs: Client creation options.

        Returns:
            A mock-transport HTTPX client.
        """
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(collector.httpx, "AsyncClient", client)
    sites = [
        {"name": f"site-{i}", "url": f"https://configured.invalid/{i}"}
        for i in range(12)
    ]
    sites.extend(
        {"name": name, "url": f"https://configured.invalid/{name}"}
        for name in ("overall-timeout", "connect-error", "read-timeout")
    )
    sites.append({"name": "invalid", "url": "file:///etc/passwd"})
    results = asyncio.run(collector.check_connectivity(sites, timeout=0.03))
    assert [result["name"] for result in results] == [site["name"] for site in sites]
    assert 1 < state.maximum <= 8
    assert state.active == 0
    assert all(result["status"] == 204 for result in results[:12])
    for result, label in zip(
        results[-4:], ("TimeoutError", "ConnectError", "ReadTimeout", "ValueError")
    ):
        assert result["status"] is None
        assert result["reason"] == label
        assert label in result["error"]
        assert result["delay"] >= 0
    assert 20 <= results[-4]["delay"] < 500


def test_connectivity_redirect_loop_is_bounded(monkeypatch):
    """Stop a redirect loop and report TooManyRedirects, not fabricated success.

    Args:
        monkeypatch: Pytest's isolated patch manager.
    """
    original_client = httpx.AsyncClient
    requested = []

    def handler(request):
        """Redirect to the same trusted configured endpoint.

        Args:
            request: Mock transport request.

        Returns:
            A redirect response.
        """
        requested.append(request)
        return httpx.Response(302, headers={"Location": "/loop"})

    def client(**kwargs):
        """Install a small redirect limit on a mock-only client.

        Args:
            **kwargs: Client creation options.

        Returns:
            An HTTPX mock client.
        """
        return original_client(
            transport=httpx.MockTransport(handler), max_redirects=2, **kwargs
        )

    monkeypatch.setattr(collector.httpx, "AsyncClient", client)
    result = asyncio.run(
        collector.check_connectivity(
            [{"name": "loop", "url": "https://configured.invalid/loop"}]
        )
    )[0]
    assert len(requested) == 3
    assert result["status"] is None
    assert result["reason"] == "TooManyRedirects"
    assert result["delay"] >= 0


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_connectivity_invalid_timeout(timeout):
    """Reject unbounded or invalid connectivity deadlines.

    Args:
        timeout: Invalid deadline value.
    """
    with pytest.raises(ValueError):
        asyncio.run(collector.check_connectivity([], timeout=timeout))


def test_connectivity_uses_proxy_only_for_opted_in_sites(monkeypatch):
    """Apply the global proxy only to sites flagged use_proxy.

    Args:
        monkeypatch: Pytest's isolated patch manager.
    """
    original_client = httpx.AsyncClient
    created = []

    def handler(request):
        """Return a headers-only success without network access.

        Args:
            request: Mock transport request.

        Returns:
            A headers-only OK response.
        """
        return httpx.Response(200)

    def client(**kwargs):
        """Record client options and inject an offline transport.

        Args:
            **kwargs: Client creation options.

        Returns:
            An HTTPX mock client.
        """
        created.append(kwargs)
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(collector.httpx, "AsyncClient", client)
    results = asyncio.run(
        collector.check_connectivity(
            [
                {"name": "direct", "url": "https://direct.invalid/"},
                {
                    "name": "proxied",
                    "url": "https://proxied.invalid/",
                    "use_proxy": True,
                },
            ],
            timeout=1,
            proxy="http://127.0.0.1:7890",
        )
    )
    assert len(created) == 2
    proxied = [options for options in created if options.get("proxy")]
    assert len(proxied) == 1
    assert proxied[0]["proxy"] == "http://127.0.0.1:7890"
    assert all(options["trust_env"] is False for options in created)
    assert [item["name"] for item in results] == ["direct", "proxied"]


def test_connectivity_opt_in_without_proxy_errors_not_direct(monkeypatch):
    """Never silently bypass a required proxy when none is configured.

    Args:
        monkeypatch: Pytest's isolated patch manager.
    """
    original_client = httpx.AsyncClient

    def handler(request):
        """Reject unexpected direct traffic.

        Args:
            request: Mock transport request.

        Raises:
            AssertionError: If a direct request is actually attempted.
        """
        raise AssertionError("A site requiring a proxy must not connect directly")

    def client(**kwargs):
        """Inject an offline transport.

        Args:
            **kwargs: Client creation options.

        Returns:
            An HTTPX mock client.
        """
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(collector.httpx, "AsyncClient", client)
    result = asyncio.run(
        collector.check_connectivity(
            [{"name": "proxied", "url": "https://proxied.invalid/", "use_proxy": True}],
            timeout=1,
        )
    )[0]
    assert result["status"] is None
    assert "proxy" in result["error"].lower()


def test_connectivity_all_direct_builds_no_proxy_client(monkeypatch):
    """Build only the direct client when nothing opts into the proxy.

    Args:
        monkeypatch: Pytest's isolated patch manager.
    """
    original_client = httpx.AsyncClient
    created = []

    def handler(request):
        """Return a headers-only success.

        Args:
            request: Mock transport request.

        Returns:
            A headers-only OK response.
        """
        return httpx.Response(204)

    def client(**kwargs):
        """Record client options and inject an offline transport.

        Args:
            **kwargs: Client creation options.

        Returns:
            An HTTPX mock client.
        """
        created.append(kwargs)
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(collector.httpx, "AsyncClient", client)
    asyncio.run(
        collector.check_connectivity(
            [{"name": "direct", "url": "https://direct.invalid/"}],
            timeout=1,
            proxy="http://127.0.0.1:7890",
        )
    )
    assert len(created) == 1
    assert "proxy" not in created[0]


def test_connectivity_invalid_proxy_reports_error(monkeypatch):
    """Surface a malformed proxy instead of probing directly.

    Args:
        monkeypatch: Pytest's isolated patch manager.
    """
    original_client = httpx.AsyncClient

    def handler(request):
        """Reject unexpected direct traffic.

        Args:
            request: Mock transport request.

        Raises:
            AssertionError: If a direct request is attempted.
        """
        raise AssertionError("Malformed proxy must not fall back to direct")

    def client(**kwargs):
        """Inject an offline transport.

        Args:
            **kwargs: Client creation options.

        Returns:
            An HTTPX mock client.
        """
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(collector.httpx, "AsyncClient", client)
    result = asyncio.run(
        collector.check_connectivity(
            [{"name": "proxied", "url": "https://proxied.invalid/", "use_proxy": True}],
            timeout=1,
            proxy="not a valid url",
        )
    )[0]
    assert result["status"] is None
    assert result["error"]


@pytest.fixture
def display_metrics(metrics, monkeypatch):
    """Provide deliberately unsorted rows and a shared, measured counter interval.

    Args:
        metrics: Deterministic system metric fixture.
        monkeypatch: Pytest's isolated patch manager.

    Returns:
        Captured sampling state, including per-device counter calls.
    """
    monkeypatch.setattr(
        collector.psutil,
        "disk_partitions",
        lambda all=False: [
            SimpleNamespace(mountpoint=mount, fstype=fs, device="/dev/ignore-me")
            for mount, fs in (
                ("/small", "ext4"),
                ("/denied", "ext4"),
                ("/large", "ext4"),
                ("/snap/test/1", "squashfs"),
            )
        ],
    )
    original_usage = collector.psutil.disk_usage

    def usage(mountpoint):
        """Return distinct percentages, preserving the inaccessible mount.

        Args:
            mountpoint: Filesystem mount being sampled.

        Returns:
            A disk usage snapshot.
        """
        if mountpoint == "/large":
            return SimpleNamespace(used=900, total=1000, percent=90.0)
        return original_usage(mountpoint)

    def disk_counters(**kwargs):
        """Sample each device once per endpoint with a reset and idle device.

        Args:
            **kwargs: psutil-compatible counter options.

        Returns:
            Ordered disk counters, independent of host hardware.
        """
        assert kwargs == {"perdisk": True, "nowrap": False}
        metrics.disk_calls += 1
        second = metrics.disk_calls % 2 == 0
        return {
            name: SimpleNamespace(
                read_bytes=100 + (read if second else 0),
                write_bytes=100 + (write if second else 0),
            )
            for name, read, write in (
                ("slow", 10, 0),
                ("idle", 0, 0),
                ("fast", 1, 99),
                ("loop0", 1000, 0),
                ("zram0", 2000, 0),
                ("reset", -100, 0),
            )
        }

    def net_counters(**kwargs):
        """Sample network rates in discovery order rather than throughput order.

        Args:
            **kwargs: psutil-compatible counter options.

        Returns:
            Ordered network counters including virtual and idle interfaces.
        """
        assert kwargs == {"pernic": True, "nowrap": False}
        metrics.net_calls += 1
        second = metrics.net_calls % 2 == 0
        return {
            name: SimpleNamespace(
                bytes_sent=100 + (sent if second else 0),
                bytes_recv=100 + (recv if second else 0),
            )
            for name, sent, recv in (
                ("eth-slow", 10, 0),
                ("idle", 0, 0),
                ("tun-fast", 1, 99),
                ("lo", 1000, 0),
                ("Loopback Pseudo", 2000, 0),
                ("本地连接 1", 3000, 0),
                ("reset", -100, 0),
            )
        }

    monkeypatch.setattr(collector.psutil, "disk_usage", usage)
    monkeypatch.setattr(collector.psutil, "disk_io_counters", disk_counters)
    monkeypatch.setattr(collector.psutil, "net_io_counters", net_counters)
    metrics.processes[5] = FakeProcess(5, "System Idle Process", mem=10000, cpu=500)
    return metrics


def test_display_options_upstream_defaults(display_metrics):
    """An empty options dictionary selects upstream defaults, not legacy filters.

    Args:
        display_metrics: Deliberately unordered metric snapshots.
    """
    data = collector.collect_status(show_gpu=False, display_options={})
    assert [row["name"] for row in data["disk_usage"]] == [
        "/large",
        "/small",
        "/snap/test/1",
        "/denied",
    ]
    assert [row["name"] for row in data["disk_io"]] == ["fast", "slow", "idle", "reset"]
    assert [row["name"] for row in data["network_io"]] == [
        "tun-fast",
        "eth-slow",
        "idle",
        "reset",
    ]
    assert [row["pid"] for row in data["process_status"]] == [2, 3, 1]
    assert data["process_status"][-1]["cpu"] == 250
    assert display_metrics.sleeps == [0.5]
    assert display_metrics.disk_calls == display_metrics.net_calls == 2
    assert data["sample_duration"] == 1
    assert data["disk_io"][0] == {"name": "fast", "read": 1, "write": 99}
    assert data["network_io"][0] == {"name": "tun-fast", "sent": 1, "recv": 99}


def test_explicit_none_keeps_legacy_filters_and_process_cpu(display_metrics):
    """None retains automatic discovery and does not apply upstream ignore defaults.

    Args:
        display_metrics: Deliberately unordered metric snapshots.
    """
    data = collector.collect_status(show_gpu=False, display_options=None)
    assert [row["name"] for row in data["disk_usage"]] == [
        "/large",
        "/small",
        "/denied",
    ]
    assert data["disk_io"] == []
    assert [row["name"] for row in data["network_io"]] == ["eth-slow"]
    assert data["process_status"][0]["name"] == "System Idle Process"
    assert data["process_status"][0]["cpu"] == 500


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"sort_parts_reverse": True}, ["/denied", "/small", "/snap/test/1", "/large"]),
        ({"sort_parts": False}, ["/small", "/denied", "/large", "/snap/test/1"]),
        (
            {"sort_parts": False, "sort_parts_reverse": True},
            ["/small", "/denied", "/large", "/snap/test/1"],
        ),
        ({"ignore_bad_parts": True}, ["/large", "/small", "/snap/test/1"]),
        ({"ignore_parts": ["large", "snap/"]}, ["/small", "/denied"]),
        (
            {"ignore_parts": ["ignore-me"]},
            ["/large", "/small", "/snap/test/1", "/denied"],
        ),
        ({"ignore_parts": ["."]}, []),
    ],
)
def test_partition_display_options(display_metrics, options, expected):
    """Search mountpoints, retain bad rows optionally, and reverse percent order.

    Args:
        display_metrics: Deliberately unordered metric snapshots.
        options: Partition filtering and sorting switches.
        expected: Expected complete mountpoint order.
    """
    data = collector.collect_status(show_gpu=False, display_options=options)
    assert [row["name"] for row in data["disk_usage"]] == expected


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"sort_disk_ios": False}, ["slow", "idle", "fast", "reset"]),
        ({"ignore_no_io_disk": True}, ["fast", "slow"]),
        ({"ignore_disk_ios": ["ast", "oop", "zram"]}, ["slow", "idle", "reset"]),
        ({"ignore_disk_ios": []}, ["zram0", "loop0", "fast", "slow", "idle", "reset"]),
    ],
)
def test_disk_io_display_options(display_metrics, options, expected):
    """Filter by regex and measured idle rates, or preserve counter order.

    Args:
        display_metrics: Deliberately unordered metric snapshots.
        options: Disk filtering and sorting switches.
        expected: Expected device order.
    """
    data = collector.collect_status(show_gpu=False, display_options=options)
    assert [row["name"] for row in data["disk_io"]] == expected
    assert display_metrics.disk_calls == display_metrics.net_calls == 2


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"sort_nets": False}, ["eth-slow", "idle", "tun-fast", "reset"]),
        ({"ignore_0b_net": True}, ["tun-fast", "eth-slow"]),
        (
            {"ignore_nets": ["ast", "^lo", "^Loopback", "本地"]},
            ["eth-slow", "idle", "reset"],
        ),
        (
            {"ignore_nets": []},
            [
                "本地连接 1",
                "Loopback Pseudo",
                "lo",
                "tun-fast",
                "eth-slow",
                "idle",
                "reset",
            ],
        ),
    ],
)
def test_network_display_options(display_metrics, options, expected):
    """Apply regex searches and measured idle filtering without legacy exclusions.

    Args:
        display_metrics: Deliberately unordered metric snapshots.
        options: Interface filtering and sorting switches.
        expected: Expected interface order.
    """
    data = collector.collect_status(show_gpu=False, display_options=options)
    assert [row["name"] for row in data["network_io"]] == expected
    assert display_metrics.disk_calls == display_metrics.net_calls == 2


@pytest.mark.parametrize("sort_rows", [True, False])
def test_display_sorting_with_explicit_allowlists(display_metrics, sort_rows):
    """Keep exact allowlists while letting explicit upstream sorting take priority.

    Args:
        display_metrics: Deliberately unordered metric snapshots.
        sort_rows: Whether rates should override administrator allowlist order.
    """
    data = collector.collect_status(
        show_gpu=False,
        disk_devices=["slow", "loop0", "fast", "slow"],
        network_interfaces=["eth-slow", "lo", "tun-fast", "eth-slow"],
        display_options={"sort_disk_ios": sort_rows, "sort_nets": sort_rows},
    )
    assert [row["name"] for row in data["disk_io"]] == (
        ["fast", "slow"] if sort_rows else ["slow", "fast"]
    )
    assert [row["name"] for row in data["network_io"]] == (
        ["tun-fast", "eth-slow"] if sort_rows else ["eth-slow", "tun-fast"]
    )


def test_process_ignore_filters_before_top_n_and_keeps_pid_identity(display_metrics):
    """Filter names before limiting, without combining independent equal names.

    Args:
        display_metrics: Deliberately unordered metric snapshots.
    """
    data = collector.collect_status(
        top_n=2,
        show_gpu=False,
        process_sort_by="cpu",
        display_options={"ignore_procs": ["Idle", "untruncated"]},
    )
    assert [row["pid"] for row in data["process_status"]] == [1, 2]
    assert [row["name"] for row in data["process_status"]] == ["python", "python"]


def test_empty_process_ignore_list_disables_default(display_metrics):
    """An explicit empty ignore list must include the default ignored process.

    Args:
        display_metrics: Deliberately unordered metric snapshots.
    """
    data = collector.collect_status(
        top_n=1, show_gpu=False, display_options={"ignore_procs": []}
    )
    assert [row["pid"] for row in data["process_status"]] == [5]


@pytest.mark.parametrize("logical_count", [8, None, 0])
def test_process_cpu_normalization_is_not_clamping(
    display_metrics, monkeypatch, logical_count
):
    """Divide by logical CPUs, retaining raw CPU if the count is unavailable.

    Args:
        display_metrics: Deliberately unordered metric snapshots.
        monkeypatch: Pytest's isolated patch manager.
        logical_count: Available logical count, zero, or unavailable.
    """
    monkeypatch.setattr(
        collector.psutil,
        "cpu_count",
        lambda logical=True: logical_count if logical else 4,
    )
    data = collector.collect_status(
        show_gpu=False,
        process_sort_by="cpu",
        display_options={"proc_cpu_max_100p": True},
    )
    assert data["process_status"][0]["pid"] == 1
    assert data["process_status"][0]["cpu"] == (31.25 if logical_count else 250)
    assert display_metrics.processes[1].calls == 2


@pytest.mark.parametrize(
    "key", ["ignore_parts", "ignore_disk_ios", "ignore_nets", "ignore_procs"]
)
def test_invalid_display_regex_fails_before_any_sampling(metrics, key):
    """Reject even unused invalid regexes upfront with their configuration key.

    Args:
        metrics: Deterministic system metric fixture.
        key: Ignore list whose regex is invalid.
    """
    with pytest.raises(ValueError, match=key):
        collector.collect_status(top_n=0, show_gpu=False, display_options={key: ["["]})
    assert metrics.sleeps == []
    assert metrics.disk_calls == metrics.net_calls == metrics.cpu_calls == 0
    assert all(proc.calls == 0 for proc in metrics.processes.values())


def test_display_options_no_hardcoded_row_limits_and_zero_process_count(
    display_metrics, monkeypatch
):
    """Retain every disk/interface row while completely disabling process work.

    Args:
        display_metrics: Deliberately unordered metric snapshots.
        monkeypatch: Pytest's isolated patch manager.
    """
    disks = {
        f"disk-{index}": SimpleNamespace(read_bytes=0, write_bytes=0)
        for index in range(30)
    }
    nets = {
        f"nic-{index}": SimpleNamespace(bytes_sent=0, bytes_recv=0)
        for index in range(30)
    }
    monkeypatch.setattr(collector.psutil, "disk_io_counters", lambda **kwargs: disks)
    monkeypatch.setattr(collector.psutil, "net_io_counters", lambda **kwargs: nets)
    monkeypatch.setattr(
        collector.psutil,
        "disk_partitions",
        lambda all=False: [
            SimpleNamespace(mountpoint=f"/mount-{index}", fstype="ext4")
            for index in range(30)
        ],
    )
    data = collector.collect_status(top_n=0, show_gpu=False, display_options={})
    assert (
        len(data["disk_io"]) == len(data["network_io"]) == len(data["disk_usage"]) == 30
    )
    assert data["process_status"] == []
    assert all(proc.calls == 0 for proc in display_metrics.processes.values())

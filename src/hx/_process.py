"""Bound process lifetimes; this is cleanup, not a security sandbox."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO


def _capture(
    proc: subprocess.Popen[bytes],
    timeout: float,
    output_limit: int,
    kill_tree: Callable[[], None],
) -> subprocess.CompletedProcess[bytes]:
    """Drain both pipes concurrently while retaining a small byte suffix."""
    assert proc.stdout is not None and proc.stderr is not None
    streams = (proc.stdout, proc.stderr)
    tails = (bytearray(), bytearray())
    errors: list[OSError] = []

    def read(stream: BinaryIO, tail: bytearray) -> None:
        try:
            with stream:
                while chunk := stream.read1(65536):
                    tail.extend(chunk)
                    del tail[: max(0, len(tail) - output_limit)]
        except OSError as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=read, args=(stream, tail), daemon=True)
        for stream, tail in zip(streams, tails, strict=True)
    ]

    def finish(seconds: float) -> None:
        deadline = time.monotonic() + seconds
        proc.wait(timeout=seconds)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                raise subprocess.TimeoutExpired(proc.args, timeout)

    for thread in threads:
        thread.start()
    try:
        finish(timeout)
    except subprocess.TimeoutExpired:
        kill_tree()
        try:
            finish(5)
        except subprocess.TimeoutExpired:
            pass
        raise subprocess.TimeoutExpired(
            proc.args, timeout, output=bytes(tails[0]), stderr=bytes(tails[1])
        ) from None
    if errors:
        raise errors[0]
    return subprocess.CompletedProcess(proc.args, proc.returncode, bytes(tails[0]), bytes(tails[1]))


def execute(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    output_limit: int = 16384,
):
    if os.name == "nt":
        return _windows(argv, cwd, env, timeout, output_limit)
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    def kill_tree() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        return _capture(proc, timeout, output_limit, kill_tree)
    finally:
        kill_tree()


def _windows(argv, cwd, env, timeout, output_limit):
    # Start suspended: descendants cannot escape the job before assignment.
    import ctypes as c
    from ctypes import wintypes as w

    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.AssignProcessToJobObject.restype = w.BOOL
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenThread.restype = w.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.ResumeThread.argtypes = [w.HANDLE]
    kernel.ResumeThread.restype = w.DWORD
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
    kernel.TerminateJobObject.restype = w.BOOL

    class Basic(c.Structure):
        _fields_ = [
            ("process_time", c.c_longlong),
            ("job_time", c.c_longlong),
            ("flags", w.DWORD),
            ("minimum", c.c_size_t),
            ("maximum", c.c_size_t),
            ("active", w.DWORD),
            ("affinity", c.c_size_t),
            ("priority", w.DWORD),
            ("scheduling", w.DWORD),
        ]

    class Io(c.Structure):
        _fields_ = [
            (name, c.c_ulonglong)
            for name in (
                "read_ops",
                "write_ops",
                "other_ops",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class Extended(c.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", Io),
            ("process_memory", c.c_size_t),
            ("job_memory", c.c_size_t),
            ("peak_process", c.c_size_t),
            ("peak_job", c.c_size_t),
        ]

    class Thread(c.Structure):
        _fields_ = [
            ("size", w.DWORD),
            ("usage", w.DWORD),
            ("tid", w.DWORD),
            ("pid", w.DWORD),
            ("base", w.LONG),
            ("delta", w.LONG),
            ("flags", w.DWORD),
        ]

    kernel.Thread32First.argtypes = [w.HANDLE, c.POINTER(Thread)]
    kernel.Thread32Next.argtypes = [w.HANDLE, c.POINTER(Thread)]
    kernel.Thread32First.restype = kernel.Thread32Next.restype = w.BOOL

    def checked(value):
        if not value:
            raise c.WinError(c.get_last_error())
        return value

    job = checked(kernel.CreateJobObjectW(None, None))
    proc = None
    try:
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        checked(kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)))
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=0x4 | 0x200 | 0x08000000,
        )
        handle = checked(kernel.OpenProcess(0x100 | 0x1, False, proc.pid))
        try:
            checked(kernel.AssignProcessToJobObject(job, handle))
        finally:
            kernel.CloseHandle(handle)
        snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        resumed = False
        try:
            entry = Thread()
            entry.size = c.sizeof(entry)
            found = kernel.Thread32First(snapshot, c.byref(entry))
            while found:
                if entry.pid == proc.pid:
                    thread = checked(kernel.OpenThread(0x2, False, entry.tid))
                    try:
                        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise c.WinError(c.get_last_error())
                        resumed = True
                    finally:
                        kernel.CloseHandle(thread)
                found = kernel.Thread32Next(snapshot, c.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
        if not resumed:
            raise OSError("could not resume the job process")

        def kill_tree() -> None:
            checked(kernel.TerminateJobObject(job, 1))

        return _capture(proc, timeout, output_limit, kill_tree)
    finally:
        kernel.CloseHandle(job)
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

"""Bound the Docker client directly; never wait for EOF from descendants via tee.

No deployment, image publication, daemon-wide kill or runtime credentials belong here.
The existing CI preflight must succeed on a separate Linux builder before main().
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_SECONDS = 1080
TAIL_BYTES = 24000


def tail(path):
    with path.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - TAIL_BYTES))
        data = stream.read(TAIL_BYTES)
    return [line[:500] for line in data.decode('utf-8', errors='replace').splitlines()[-60:]]


def save(path, report):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    temporary.replace(path)


def stop_client(process, grace):
    """Signal only the process/session started here, with bounded waits."""
    if process.poll() is not None:
        return False, True
    forced = False
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        forced = True
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return forced, False
    return forced, True


def supervise(command, reports, source_sha, *, seconds=MAX_SECONDS, cwd=None,
              heartbeat=15, kill_grace=5, handle_signals=True):
    if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS:
        raise ValueError('build deadline must be 1..1080 seconds')
    if not re.fullmatch('[0-9a-f]{40}', source_sha):
        raise ValueError('source revision must be a lowercase 40-digit Git SHA')
    reports = Path(reports)
    reports.mkdir(parents=True, exist_ok=True)
    log = reports.parent/'gateway-build.log'
    report_path = reports/'build.json'
    started = time.monotonic()
    received = []
    previous = {}
    report = {'successful':False, 'state':'starting', 'exit_code':None,
              'timeout_exit':False, 'forced_kill_or_signal_exit':False,
              'source_sha':source_sha, 'started_at':datetime.now(timezone.utc).isoformat(),
              'deadline_seconds':seconds, 'deployment':False,
              'real_credentials_provided':False, 'build_tail':[]}
    save(report_path, report)
    def on_signal(number, frame):
        if not received:
            received.append(number)
    process = None
    code = 1
    if handle_signals:
        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, on_signal)
    try:
        # A regular file removes the extra pipe reader and its inherited-EOF wait.
        # Close it in this process before reading a bounded tail, never communicate().
        with log.open('wb', buffering=0) as stream:
            options = {'start_new_session':True} if os.name == 'posix' else {}
            try:
                process = subprocess.Popen(command, cwd=cwd, stdout=stream,
                                           stderr=subprocess.STDOUT, close_fds=True, **options)
            except OSError as error:
                code = 127
                report['error_type'] = type(error).__name__
            else:
                report['state'] = 'running'
                save(report_path, report)
                next_heartbeat = started
                while True:
                    elapsed = time.monotonic()-started
                    # A pending cancellation takes precedence over an exit-0 race.
                    if received or elapsed >= seconds:
                        timed_out = not received
                        forced, stopped = stop_client(process, kill_grace)
                        code = 124 if timed_out else 128+received[0]
                        report.update(timeout_exit=timed_out, signal=received[0] if received else None,
                                      client_termination_confirmed=stopped,
                                      forced_kill_or_signal_exit=forced)
                        break
                    result = process.poll()
                    if result is not None:
                        code = result if result >= 0 else 128-result
                        break
                    if elapsed >= next_heartbeat:
                        report.update(elapsed_seconds=round(elapsed,3), build_tail=tail(log))
                        save(report_path, report)
                        print('Gateway build: %.1fs / %ds; last output: %s' %
                              (elapsed, seconds, (report['build_tail'] or ['(none yet)'])[-1]), flush=True)
                        next_heartbeat = elapsed+heartbeat
                    time.sleep(min(0.1, max(0.001, seconds-elapsed)))
    except BaseException:
        if process is not None and process.poll() is None:
            stop_client(process, kill_grace)
        raise
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        report.update(state='completed', exit_code=code, successful=code==0,
                      elapsed_seconds=round(time.monotonic()-started,3),
                      client_return_code=process.returncode if process is not None else None,
                      build_tail=tail(log) if log.is_file() else [])
        save(report_path, report)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports', type=Path, required=True)
    args = parser.parse_args()
    raw = os.environ.get('GATEWAY_CI_BUILD_SECONDS', str(MAX_SECONDS))
    if not re.fullmatch('[1-9][0-9]{0,3}', raw) or int(raw)>MAX_SECONDS:
        print('Invalid build deadline; expected 1..1080 seconds', file=sys.stderr)
        return 64
    sha = os.environ.get('SOURCE_GIT_SHA','')
    if not re.fullmatch('[0-9a-f]{40}', sha):
        print('Invalid source revision', file=sys.stderr)
        return 64
    command = ['docker','build','--platform','linux/amd64','--pull=false',
               '--build-arg','SOURCE_GIT_SHA='+sha,
               '--label','org.opencontainers.image.revision='+sha,
               '-t','gateway-corporate-ci','.']
    return supervise(command,args.reports,sha,seconds=int(raw),
                     cwd=Path(__file__).resolve().parents[1])


if __name__ == '__main__':
    sys.exit(main())

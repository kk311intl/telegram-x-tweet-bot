"""Shared size-bounded video fallback for gallery-dl and yt-dlp."""
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 kk311intl. See LICENSE.downloader.

import os
import sys
import tempfile
import threading
from pathlib import Path

import yt_dlp

NETWORK_TIMEOUT_EXIT_CODE = 124


class VideoTooLarge(Exception):
    pass


def install_network_deadlines():
    """Only used in the disposable extractor process; never patch the Bot."""
    import requests
    from yt_dlp.networking.common import RequestHandler
    from yt_dlp.downloader.external import FFmpegFD

    seconds = min(60, max(0.01, float(os.environ.get("X_TWEET_NETWORK_TIMEOUT", "60"))))

    class ExtractorDeadline:
        def __init__(self, seconds):
            self.lock = threading.Lock()
            self.done = False
            self.timer = threading.Timer(seconds, self.abort)
            self.timer.daemon = True
            self.timer.start()

        def close(self):
            with self.lock:
                self.done = True
            self.timer.cancel()

        def abort(self):
            with self.lock:
                if not self.done:
                    # Do not let a library catch a timeout and retry the job.
                    # The parent reaps this process and its downloader children.
                    os._exit(NETWORK_TIMEOUT_EXIT_CODE)

    def bounded_response(response, deadline):
        source = response.raw if isinstance(response, requests.Response) else response
        for name in ("read", "read1"):
            read = getattr(source, name, None)
            if not callable(read):
                continue
            def bounded_read(*args, _read=read, _all=name == "read", **kwargs):
                try:
                    data = _read(*args, **kwargs)
                except BaseException:
                    deadline.close()
                    raise
                amount = args[0] if args else kwargs.get("amt", kwargs.get("size"))
                if amount != 0 and (not data or (_all and (amount is None or amount < 0))):
                    deadline.close()
                return data
            setattr(source, name, bounded_read)
        original_close = response.close
        def close():
            try:
                original_close()
            finally:
                deadline.close()
        response.close = close
        return response

    def bounded_send(original):
        def send(self, *args, **kwargs):
            deadline = ExtractorDeadline(seconds)
            try:
                response = original(self, *args, **kwargs)
            except BaseException as error:
                response = getattr(error, "response", None)
                if response is not None:
                    bounded_response(response, deadline)
                else:
                    deadline.close()
                raise
            if isinstance(response, requests.Response) and not kwargs.get("stream"):
                deadline.close()
                return response
            return bounded_response(response, deadline)
        return send

    requests.Session.send = bounded_send(requests.Session.send)
    RequestHandler.send = bounded_send(RequestHandler.send)
    original_ffmpeg = FFmpegFD._call_downloader
    def ffmpeg(*args, **kwargs):
        deadline = ExtractorDeadline(seconds)
        try:
            return original_ffmpeg(*args, **kwargs)
        finally:
            deadline.close()
    FFmpegFD._call_downloader = ffmpeg


class YoutubeDL(yt_dlp.YoutubeDL):
    def __init__(self, params=None, *args, **kwargs):
        options = {**(params or {}), "buffersize": 65536, "noresizebuffer": True}
        limit = min(int(options.get("max_filesize") or 50_000_000), 50_000_000)
        # External FFmpeg only reports progress on completion. Stop its output
        # just above our acceptance limit so a truncated file cannot pass.
        options["external_downloader_args"] = {"ffmpeg_o": ["-flush_packets", "1", "-fs", str(limit + 1)]}
        options["skip_unavailable_fragments"] = False
        super().__init__(options, *args, **kwargs)

    def process_info(self, info):
        limit = min(int(self.params.get("max_filesize") or 50_000_000), 50_000_000)
        formats = [fmt for fmt in info.get("formats") or [info] if fmt.get("vcodec") != "none"]
        selector = self.build_format_selector(f"best[filesize<=?{limit}]")
        format_keys = {key for fmt in formats for key in fmt}
        metadata = {key: value for key, value in info.items() if key not in format_keys}
        # Keep post metadata when a single direct URL has no format list.
        if not info.get("formats"):
            metadata = dict(info)
        original_template = self.params.get("outtmpl")
        original_hooks = self._progress_hooks[:]
        previous_status = self._download_retcode

        def check_size(progress):
            if progress.get("downloaded_bytes", 0) > limit:
                raise VideoTooLarge()

        self.add_progress_hook(check_size)
        try:
            while formats:
                selected = self._select_formats(formats, selector)
                if not selected:
                    break
                fmt = selected[-1]
                formats = [item for item in formats if item.get("url") != fmt.get("url")]
                attempt = {**metadata, **fmt}
                target = Path(self.prepare_filename(attempt))
                target.parent.mkdir(parents=True, exist_ok=True)
                # Each quality starts from zero; an oversized partial must never
                # be resumed into a different rendition or left on disk.
                with tempfile.TemporaryDirectory(prefix=".video-", dir=target.parent) as folder:
                    temporary = Path(folder) / target.name
                    self.params["outtmpl"] = {"default": str(temporary).replace("%", "%%")}
                    try:
                        super().process_info(attempt)
                        output = Path(attempt.get("filepath") or temporary)
                        if output.is_file() and 0 < output.stat().st_size <= limit:
                            os.replace(output, target)
                            attempt.update(filepath=str(target), _filename=str(target))
                            info.update(attempt)
                            self._download_retcode = previous_status
                            return
                    except (VideoTooLarge, yt_dlp.utils.DownloadError):
                        pass
                    finally:
                        self.params["outtmpl"] = original_template
                self.to_screen("[download] Trying the next video quality")
            raise yt_dlp.utils.DownloadError("No video quality could be downloaded within the size limit")
        finally:
            self._progress_hooks[:] = original_hooks


if __name__ == "__main__":
    install_network_deadlines()
    if sys.argv[1:2] == ["--gallery-dl"]:
        import gallery_dl
        del sys.argv[1]
        raise SystemExit(gallery_dl.main())
    else:
        yt_dlp.YoutubeDL = YoutubeDL
        yt_dlp.main()

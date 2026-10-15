"""Shared size-bounded video fallback for gallery-dl and yt-dlp."""

import os
import tempfile
from pathlib import Path

import yt_dlp


class VideoTooLarge(Exception):
    pass


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
    yt_dlp.YoutubeDL = YoutubeDL
    yt_dlp.main()

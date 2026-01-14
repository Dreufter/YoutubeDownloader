import sys
import os
import time
from pathlib import Path
from datetime import datetime
import imageio_ffmpeg
from yt_dlp import YoutubeDL
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QComboBox, QCheckBox, QLabel, QProgressBar,
    QFileDialog, QTextEdit
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont


# ==================== CONFIGURATION ====================
DOWNLOAD_FOLDER = str(Path.home() / "YouTubeDownloader-Downloads")
Path(DOWNLOAD_FOLDER).mkdir(parents=True, exist_ok=True)

QUALITY_MAP = {
    "Best": "bestvideo+bestaudio[acodec!=opus]/best",
    "1080p": "bestvideo[height<=1080]+bestaudio[acodec!=opus]/best[height<=1080]/best",
    "720p": "bestvideo[height<=720]+bestaudio[acodec!=opus]/best[height<=720]/best",
    "480p": "bestvideo[height<=480]+bestaudio[acodec!=opus]/best[height<=480]/best",
}

AUDIO_FORMATS = ["MP3", "M4A", "WAV", "OPUS"]


# ==================== DOWNLOAD WORKER THREAD ====================
class DownloadWorker(QThread):
    progress = pyqtSignal(int)
    speed = pyqtSignal(str)
    eta = pyqtSignal(str)
    current_file = pyqtSignal(str)
    file_started = pyqtSignal(str)
    finished = pyqtSignal(dict)
    cancelled = pyqtSignal()
    error = pyqtSignal(str)
    
    def __init__(self, url, mode, quality, audio_format, is_playlist, download_folder):
        super().__init__()
        self.url = url
        self.mode = mode
        self.quality = quality
        self.audio_format = audio_format
        self.is_playlist = is_playlist
        self.download_folder = download_folder
        self.is_paused = False
        self.is_cancelled = False
        self.current_proc = None
    
    def run(self):
        try:
            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
            
            ydl_opts = {
                "ffmpeg_location": ffmpeg_path,
                "outtmpl": os.path.join(self.download_folder, "%(title)s.%(ext)s"),
                "restrictfilenames": True,
                "sanitize_filename": True,
                "quiet": False,
                "no_warnings": False,
                "progress_hooks": [self.progress_hook],
                "socket_timeout": 30,
                "noplaylist": not self.is_playlist,
                "ignoreerrors": True,
            }
            
            if self.mode == "audio":
                ydl_opts["format"] = "bestaudio/best"
                ydl_opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": self.audio_format.lower(),
                    "preferredquality": "192",
                }]
            else:
                ydl_opts["format"] = QUALITY_MAP[self.quality]
                ydl_opts["merge_output_format"] = "mp4"
                ydl_opts["postprocessors"] = [{
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                }]
            
            # First perform a fast (flat) extraction to enumerate playlist items quickly
            ydl_flat_opts = ydl_opts.copy()
            ydl_flat_opts["extract_flat"] = True
            # use a lightweight extractor to fetch the list of entries fast
            try:
                with YoutubeDL(ydl_flat_opts) as ydl_flat:
                    flat_info = ydl_flat.extract_info(self.url, download=False)
            except Exception as e:
                # Fall back to normal extraction error
                self.error.emit(f"Failed to extract playlist info: {str(e)}")
                return

            entries_flat = flat_info.get("entries") if isinstance(flat_info, dict) and "entries" in flat_info else [flat_info]
            if not entries_flat:
                entries_flat = [flat_info]

            total = len(entries_flat)
            self.current_file.emit(f"Found {total} items")

            # Now perform detailed download pass, emitting queued titles as we go
            stats = {"successful": 0, "failed": 0, "total": total}
            with YoutubeDL(ydl_opts) as ydl:
                for idx, entry in enumerate(entries_flat, 1):
                    if self.is_cancelled:
                        break

                    if entry is None:
                        stats["failed"] += 1
                        continue

                    # entry from flat extraction may only contain 'url' (id). Normalize to a full URL
                    video_id = entry.get("id") or entry.get("url")
                    video_url = entry.get("webpage_url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None)

                    # Try to retrieve title quickly (may fail if extraction needs JS)
                    title = entry.get("title") or video_id or "Unknown"
                    try:
                        info_full = ydl.extract_info(video_url, download=False)
                        title = info_full.get("title", title)
                    except Exception:
                        # keep best-effort title from flat entry
                        pass

                    self.current_file.emit(f"Queued [{idx}/{total}] {title}")
                    # Don't emit 0% here to avoid showing a premature second pass
                    # Progress updates will come from `progress_hook` and final 100% is
                    # emitted after `ydl.download()` completes.
                    self.last_progress_logged = -1  # Reset tracking for new file

                    if self.is_cancelled:
                        break

                    self.file_100_logged = False  # Reset 100% flag for new file
                    # notify UI that this file's download is starting (unblocks held progress)
                    try:
                        self.file_started.emit(title)
                        ydl.download([video_url])
                        self.progress.emit(100)
                        stats["successful"] += 1
                        self.current_file.emit(f"✓ Downloaded: {title}")
                    except Exception as e:
                        stats["failed"] += 1
                        self.current_file.emit(f"✗ Failed: {title}")

            # Cleanup and finish
            self.cleanup_partial_files()
            if self.is_cancelled:
                self.cancelled.emit()
            else:
                self.finished.emit(stats)
        
        except Exception as e:
            self.error.emit(f"Download failed: {str(e)}")
    
    def progress_hook(self, d):
        if self.is_cancelled:
            return
        
        while self.is_paused:
            if self.is_cancelled:
                return
            time.sleep(0.1)
        
        # track ffmpeg/proc handle if provided so we can terminate on cancel
        if "proc" in d:
            try:
                self.current_proc = d.get("proc")
            except Exception:
                self.current_proc = None

        status = d.get("status")
        
        if status == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            speed = d.get("speed", 0)
            eta = d.get("eta", 0)
            
            if total > 0:
                percent = int((downloaded / total) * 100)
                # Don't emit 100% from here - let the download completion emit it once
                if percent < 100:
                    self.progress.emit(percent)
                self.speed.emit(self.format_size(speed) + "/s")
                self.eta.emit(self.format_time(eta))
        
        elif status == "finished":
            # Don't emit 100% here - on_progress will handle logging it once
            pass

    def cancel(self):
        """Request cancellation and try to terminate any running subprocess."""
        self.is_cancelled = True
        # if paused, unpause so loops can exit promptly
        self.is_paused = False
        try:
            if self.current_proc is not None:
                # proc is a subprocess.Popen-like object; try terminate then kill
                try:
                    self.current_proc.terminate()
                except Exception:
                    pass
                try:
                    self.current_proc.kill()
                except Exception:
                    pass
        except Exception:
            pass
    
    @staticmethod
    def format_size(bytes_size):
        if not bytes_size:
            return "0 B"
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_size < 1024:
                return f"{bytes_size:.2f} {unit}"
            bytes_size /= 1024
        return f"{bytes_size:.2f} TB"
    
    @staticmethod
    def format_time(seconds):
        if not seconds:
            return "--:--:--"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    
    def cleanup_partial_files(self):
        for root, dirs, files in os.walk(self.download_folder):
            for file in files:
                if file.endswith(".part"):
                    try:
                        os.remove(os.path.join(root, file))
                    except:
                        pass


# ==================== MAIN WINDOW ====================
class YouTubeDownloaderApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.download_thread = None
        self.last_progress_logged = -1
        self.last_speed_logged = ""
        self.last_eta_logged = ""
        self.file_100_logged = False
        self.file_100_logged = False
        self.hold_progress = False  # keep progress bar at 100% until next queued file
        self.init_ui()
        self.setStyleSheet(self.get_stylesheet())
    
    def init_ui(self):
        self.setWindowTitle("YouTube Downloader Pro")
        self.setGeometry(100, 100, 850, 700)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout()
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(20, 20, 20, 20)
        
        # Title
        title = QLabel("YouTube Downloader Pro")
        title_font = QFont("Sans Serif", 18, QFont.Bold)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(title)
        
        # URL Input
        url_layout = QHBoxLayout()
        url_label = QLabel("URL:")
        url_label.setMinimumWidth(80)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste YouTube URL here...")
        url_layout.addWidget(url_label)
        url_layout.addWidget(self.url_input)
        main_layout.addLayout(url_layout)
        
        # Quality Selection
        quality_layout = QHBoxLayout()
        quality_label = QLabel("Quality:")
        quality_label.setMinimumWidth(80)
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(list(QUALITY_MAP.keys()))
        quality_layout.addWidget(quality_label)
        quality_layout.addWidget(self.quality_combo)
        quality_layout.addStretch()
        main_layout.addLayout(quality_layout)
        
        # Audio Format
        audio_layout = QHBoxLayout()
        audio_label = QLabel("Audio:")
        audio_label.setMinimumWidth(80)
        self.audio_combo = QComboBox()
        self.audio_combo.addItems(AUDIO_FORMATS)
        audio_layout.addWidget(audio_label)
        audio_layout.addWidget(self.audio_combo)
        audio_layout.addStretch()
        main_layout.addLayout(audio_layout)
        
        # Options
        options_layout = QHBoxLayout()
        self.playlist_check = QCheckBox("Download Playlist")
        options_layout.addWidget(self.playlist_check)
        options_layout.addStretch()
        main_layout.addLayout(options_layout)
        
        # Folder Selection
        folder_layout = QHBoxLayout()
        self.folder_btn = QPushButton("Select Folder")
        self.folder_btn.clicked.connect(self.select_folder)
        self.folder_label = QLabel(f"Folder: {DOWNLOAD_FOLDER}")
        folder_layout.addWidget(self.folder_btn)
        folder_layout.addWidget(self.folder_label)
        folder_layout.addStretch()
        main_layout.addLayout(folder_layout)
        
        # Download Buttons
        button_layout = QHBoxLayout()
        self.download_video_btn = QPushButton("⬇ Download Video")
        self.download_video_btn.setMinimumHeight(40)
        self.download_video_btn.clicked.connect(lambda: self.start_download("video"))
        
        self.download_audio_btn = QPushButton("♫ Download Audio")
        self.download_audio_btn.setMinimumHeight(40)
        self.download_audio_btn.clicked.connect(lambda: self.start_download("audio"))
        
        button_layout.addWidget(self.download_video_btn)
        button_layout.addWidget(self.download_audio_btn)
        main_layout.addLayout(button_layout)
        
        # Current File Label
        self.current_file_label = QLabel("")
        self.current_file_label.setStyleSheet("color: #00ff99; font-weight: bold;")
        main_layout.addWidget(self.current_file_label)
        
        # Progress Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setMinimumHeight(25)
        main_layout.addWidget(self.progress_bar)
        
        # Status Info
        status_layout = QHBoxLayout()
        self.status_label = QLabel("Ready")
        self.speed_label = QLabel("Speed: -")
        self.eta_label = QLabel("ETA: -")
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        status_layout.addWidget(self.speed_label)
        status_layout.addWidget(self.eta_label)
        main_layout.addLayout(status_layout)
        
        # Control Buttons
        control_layout = QHBoxLayout()
        self.pause_btn = QPushButton("⏸️ Pause")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.pause_btn.setEnabled(False)
        
        self.cancel_btn = QPushButton("⏹ Cancel")
        self.cancel_btn.clicked.connect(self.cancel_download)
        self.cancel_btn.setEnabled(False)
        
        control_layout.addStretch()
        control_layout.addWidget(self.pause_btn)
        control_layout.addWidget(self.cancel_btn)
        main_layout.addLayout(control_layout)
        
        # Log Display
        log_label = QLabel("Download Log:")
        main_layout.addWidget(log_label)
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setMaximumHeight(150)
        main_layout.addWidget(self.log_display)
        
        central_widget.setLayout(main_layout)
    
    def get_stylesheet(self):
        return """
            QMainWindow {
                background-color: #1e1e1e;
                color: #ffffff;
            }
            QLabel {
                color: #ffffff;
            }
            QLineEdit, QComboBox {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #444444;
                border-radius: 4px;
                padding: 5px;
            }
            QPushButton {
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 8px 15px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1084d7;
            }
            QPushButton:pressed {
                background-color: #006ca3;
            }
            QPushButton:disabled {
                background-color: #555555;
                color: #888888;
            }
            QProgressBar {
                background-color: #2d2d2d;
                border: 1px solid #444444;
                border-radius: 4px;
                text-align: center;
                color: #ffffff;
            }
            QProgressBar::chunk {
                background-color: #00c050;
                border-radius: 2px;
            }
            QCheckBox {
                color: #ffffff;
                spacing: 5px;
            }
            QTextEdit {
                background-color: #2d2d2d;
                color: #00ff99;
                border: 1px solid #444444;
                border-radius: 4px;
            }
        """
    
    def start_download(self, mode):
        url = self.url_input.text().strip()
        
        if not url:
            # Log instead of showing a GUI prompt
            self.log_display.append(f"[{datetime.now().strftime('%H:%M:%S')}] ❗ Please enter a YouTube URL")
            return

        # Log start of download (timestamped) so user sees immediate feedback
        ts = datetime.now().strftime('%H:%M:%S')
        emoji = "🎬" if mode == "video" else "🎵"
        self.log_display.append(f"[{ts}] {emoji} Starting {mode} download...")

        self.download_video_btn.setEnabled(False)
        self.download_audio_btn.setEnabled(False)
        self.url_input.setEnabled(False)
        # lock quality/audio selection while downloading
        self.quality_combo.setEnabled(False)
        self.audio_combo.setEnabled(False)
        self.playlist_check.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.folder_btn.setEnabled(False)
        
        self.download_thread = DownloadWorker(
            url, mode, self.quality_combo.currentText(),
            self.audio_combo.currentText(), self.playlist_check.isChecked(),
            DOWNLOAD_FOLDER
        )
        
        self.download_thread.progress.connect(self.on_progress)
        self.download_thread.speed.connect(self.on_speed)
        self.download_thread.eta.connect(self.on_eta)
        self.download_thread.current_file.connect(self.on_current_file)
        self.download_thread.cancelled.connect(self.on_download_cancelled)
        self.download_thread.finished.connect(self.on_download_finished)
        self.download_thread.error.connect(self.on_download_error)
        self.download_thread.start()
    
    def on_progress(self, value):
        # Ignore zero-percent updates entirely to avoid flicker/reset visuals.
        if value == 0:
            return

        # If we're holding the bar at 100%, only release on the first meaningful
        # non-zero update for the next download.
        if self.hold_progress:
            if value == 100:
                # still finished for previous file; ignore
                return
            # non-zero progress indicates the new download actually started
            self.hold_progress = False

        self.progress_bar.setValue(value)
        self.status_label.setText(f"Progress: {value}%")

        # When we hit 100%, lock the bar until the next actual start
        if value == 100:
            if not self.file_100_logged:
                self.log_display.append(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ File complete (100%)")
                self.file_100_logged = True
            self.hold_progress = True
    
    def on_speed(self, speed):
        self.speed_label.setText(f"Speed: {speed}")
    
    def on_eta(self, eta):
        self.eta_label.setText(f"ETA: {eta}")
        # Update internal ETA tracking but do not append ETA entries to the download log
        # to avoid excessive/duplicate ETA lines.
        self.last_eta_logged = eta
    
    def on_current_file(self, filename):
        self.current_file_label.setText(filename)
        ts = datetime.now().strftime('%H:%M:%S')
        # Beautify various worker messages using emojis
        if filename.startswith("Queued"):
            # New file queued — reset internal flags but DO NOT release the held
            # 100% visual state until the next real non-zero progress arrives.
            self.file_100_logged = False
            self.log_display.append(f"[{ts}] 📥 {filename}")
        elif filename.startswith("Found"):
            self.log_display.append(f"[{ts}] 📋 {filename}")
        elif "Downloaded" in filename or filename.startswith("✓"):
            # normalize any leading checkmark
            nice = filename.replace("✓ ", "").replace("Downloaded:", "Downloaded:")
            self.log_display.append(f"[{ts}] ✅ {nice}")
        elif "Failed" in filename or filename.startswith("✗"):
            nice = filename.replace("✗ ", "")
            self.log_display.append(f"[{ts}] ❌ {nice}")
        else:
            self.log_display.append(f"[{ts}] {filename}")
    
    def on_download_finished(self, stats):
        self.log_display.append(
            f"[{datetime.now().strftime('%H:%M:%S')}] 🎉 Complete! "
            f"Successful: {stats['successful']}, Failed: {stats['failed']}"
        )
        # Use log entry instead of popup
        self.log_display.append(f"[{datetime.now().strftime('%H:%M:%S')}] ℹ️ Summary: Successful: {stats['successful']}, Failed: {stats['failed']}")
        self.reset_ui()
    
    def on_download_error(self, error):
        self.log_display.append(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Error: {error}")
        self.reset_ui()

    def on_download_cancelled(self):
        self.log_display.append(f"[{datetime.now().strftime('%H:%M:%S')}] 🛑 Download cancelled")
        self.reset_ui()
    
    def toggle_pause(self):
        if self.download_thread:
            self.download_thread.is_paused = not self.download_thread.is_paused
            ts = datetime.now().strftime('%H:%M:%S')
            if self.download_thread.is_paused:
                # show resume label on button when paused
                self.pause_btn.setText("⏯️ Resume")
                self.log_display.append(f"[{ts}] ⏸️ Paused")
            else:
                # show pause label on button when running
                self.pause_btn.setText("⏸️ Pause")
                # Use play/pause emoji for resume which renders consistently
                self.log_display.append(f"[{ts}] ⏯️ Resumed")
    
    def cancel_download(self):
        if self.download_thread:
            # request cancellation and attempt to terminate any running subprocess
            try:
                self.download_thread.cancel()
            except Exception:
                try:
                    self.download_thread.is_cancelled = True
                except Exception:
                    pass
            self.log_display.append(f"[{datetime.now().strftime('%H:%M:%S')}] 🛑 Cancelling download...")
            # keep controls disabled until worker signals cancelled/finished
            self.download_video_btn.setEnabled(False)
            self.download_audio_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)
            self.status_label.setText("Cancelling...")
    
    def select_folder(self):
        global DOWNLOAD_FOLDER
        folder = QFileDialog.getExistingDirectory(self, "Select Download Folder", DOWNLOAD_FOLDER)
        if folder:
            DOWNLOAD_FOLDER = folder
            self.folder_label.setText(f"Folder: {DOWNLOAD_FOLDER}")
            self.log_display.append(f"[{datetime.now().strftime('%H:%M:%S')}] 📁 Download folder changed to: {DOWNLOAD_FOLDER}")
    
    def reset_ui(self):
        self.download_video_btn.setEnabled(True)
        self.download_audio_btn.setEnabled(True)
        self.url_input.setEnabled(True)
        # re-enable quality/audio selection
        self.quality_combo.setEnabled(True)
        self.audio_combo.setEnabled(True)
        self.playlist_check.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.folder_btn.setEnabled(True)
        self.pause_btn.setText("⏸ Pause")
        self.progress_bar.setValue(0)
        self.status_label.setText("Ready")
        self.speed_label.setText("Speed: -")
        self.eta_label.setText("ETA: -")
        self.current_file_label.setText("")


# ==================== MAIN ====================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = YouTubeDownloaderApp()
    window.show()
    sys.exit(app.exec_())

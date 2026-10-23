"""
Settings tab for TikHub Downloader with enhanced functionality
"""

import logging
import os
import re
import tkinter as tk
import webbrowser
import threading
from tkinter import filedialog, messagebox

import httpx
import ttkbootstrap as ttk

from downloader.constants import ABOUT_TEXT_CN, ABOUT_TEXT_EN
from downloader.constants import APP_VERSION
from downloader.locales.translate import Translator


class SettingsTab:
    """Settings tab UI and functionality"""

    def __init__(self, parent, app):
        """Initialize the settings tab

        Args:
            parent: Parent widget
            app: Application instance
        """
        # Set logging level for debugging
        logging.basicConfig(level=logging.INFO)

        self.app = app
        self.frame = ttk.Frame(parent)

        # Save reference to language dropdown for future updates
        self.language_dropdown = None

        # Version information
        self.current_version = APP_VERSION

        # Update check URL
        self.update_check_url = self.app.config.get('update_check_url')

        # Get translator from app or create a new one if not available
        self.translator = getattr(self.app, 'translator', Translator(self.app.config.get('language', 'en')))

        # Settings
        self.auto_open_var = tk.BooleanVar(value=self.app.config.get('auto_open_folder', True))
        self.skip_existing_var = tk.BooleanVar(value=self.app.config.get('skip_existing', True))
        self.rename_with_desc_var = tk.BooleanVar(value=self.app.config.get('rename_with_desc', False))
        self.postprocess_enabled_var = tk.BooleanVar(
            value=self.app.config.get('postprocess_enabled', self.app.config.get('postprocess_mirror_trim', False))
        )
        self.postprocess_trim_seconds_var = tk.StringVar(
            value=str(self.app.config.get('postprocess_trim_start_seconds', 1.5))
        )
        self.postprocess_mirror_video_var = tk.BooleanVar(
            value=self.app.config.get('postprocess_mirror_video', True)
        )
        self.postprocess_delete_original_var = tk.BooleanVar(
            value=self.app.config.get('postprocess_delete_original', False)
        )
        self.ffmpeg_path_var = tk.StringVar(value=self.app.config.get('ffmpeg_path', ''))
        self.subtitle_cover_enabled_var = tk.BooleanVar(
            value=self.app.config.get('subtitle_cover_enabled', False)
        )
        self.subtitle_cover_workspace_var = tk.StringVar(
            value=self.app.config.get('subtitle_cover_workspace') or self._guess_subtitle_cover_workspace()
        )

        # Theme setting
        self.theme_var = tk.StringVar(value=self.app.config.get('theme', 'system'))

        # Language setting
        current_language = self.app.config.get('language', 'en')
        self.language_var = tk.StringVar(value=current_language)

        # About language setting
        self.about_language = tk.StringVar(value="en")

        # Log initial language setting for debugging
        logging.info(f"SettingsTab initialized with language: {current_language}")

        # Create UI components
        self._create_widgets()

    def _create_widgets(self):
        """Create tab UI components"""
        # API key settings
        api_frame = ttk.LabelFrame(self.frame, text=self.translator.translate("settings_tab", "api_settings"))
        api_frame.pack(fill=tk.X, padx=10, pady=10)

        api_key_label = ttk.Label(api_frame, text=self.translator.translate("settings_tab", "api_key_label"))
        api_key_label.grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)

        self.api_key_var = tk.StringVar()
        self.api_key_var.set(self.app.client.api_key if self.app.client.api_key else "")
        api_key_entry = ttk.Entry(api_frame, textvariable=self.api_key_var, width=40, show="*")
        api_key_entry.grid(row=0, column=1, padx=5, pady=5, sticky=tk.W + tk.E)

        paste_button = ttk.Button(
            api_frame,
            text=self.translator.translate("settings_tab", "paste_button"),
            command=self._paste_api_key
        )
        paste_button.grid(row=0, column=2, padx=5, pady=5)

        clear_button = ttk.Button(
            api_frame,
            text=self.translator.translate("settings_tab", "clear_button") or "Clear",
            command=self._clear_api_key
        )
        clear_button.grid(row=0, column=3, padx=5, pady=5)

        show_key_button = ttk.Button(
            api_frame,
            text=self.translator.translate("settings_tab", "show_button"),
            command=lambda: self._toggle_show_password(api_key_entry)
        )
        show_key_button.grid(row=0, column=4, padx=5, pady=5)

        api_key_info = ttk.Label(api_frame, text=self.translator.translate("settings_tab", "api_key_info"))
        api_key_info.grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)

        open_tikhub_button = ttk.Button(
            api_frame,
            text=self.translator.translate("settings_tab", "open_tikhub_button"),
            command=lambda: webbrowser.open("https://user.tikhub.io/users/api_keys")
        )
        open_tikhub_button.grid(row=1, column=2, padx=5, pady=5, columnspan=3)

        # Add API URL configuration
        api_url_label = ttk.Label(api_frame, text=self.translator.translate("settings_tab", "api_url_label") or "API URL:")
        api_url_label.grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)

        self.api_url_var = tk.StringVar()
        self.api_url_var.set(self.app.config.get('api_base_url', "https://api.tikhub.io"))
        api_url_entry = ttk.Entry(api_frame, textvariable=self.api_url_var, width=40)
        api_url_entry.grid(row=2, column=1, padx=5, pady=5, sticky=tk.W + tk.E)

        reset_url_button = ttk.Button(
            api_frame,
            text=self.translator.translate("settings_tab", "reset_button") or "Reset",
            command=self._reset_api_url
        )
        reset_url_button.grid(row=2, column=2, padx=5, pady=5)

        save_api_button = ttk.Button(
            api_frame,
            text=self.translator.translate("settings_tab", "save_api_button"),
            command=self._save_api_key
        )
        save_api_button.grid(row=3, column=1, padx=5, pady=5)

        # Download path settings
        path_frame = ttk.LabelFrame(self.frame, text=self.translator.translate("settings_tab", "download_settings"))
        path_frame.pack(fill=tk.X, padx=10, pady=10)

        path_label = ttk.Label(path_frame, text=self.translator.translate("settings_tab", "download_path_label"))
        path_label.grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)

        self.path_var = tk.StringVar()
        self.path_var.set(self.app.download_path)
        path_entry = ttk.Entry(path_frame, textvariable=self.path_var, width=40)
        path_entry.grid(row=0, column=1, padx=5, pady=5, sticky=tk.W + tk.E)

        browse_button = ttk.Button(
            path_frame,
            text=self.translator.translate("settings_tab", "browse_button"),
            command=self._browse_folder
        )
        browse_button.grid(row=0, column=2, padx=5, pady=5)

        # Theme settings
        theme_frame = ttk.LabelFrame(
            self.frame,
            text=self.translator.translate("settings_tab", "theme_settings") or "Theme Settings"
        )
        theme_frame.pack(fill=tk.X, padx=10, pady=10)

        theme_label = ttk.Label(
            theme_frame,
            text=self.translator.translate("settings_tab", "theme_label") or "Application Theme:"
        )
        theme_label.grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)

        # Radio buttons for theme selection
        ttk.Radiobutton(
            theme_frame,
            text=self.translator.translate("settings_tab", "theme_light") or "Light",
            variable=self.theme_var,
            value="light"
        ).grid(row=0, column=1, padx=5, pady=5)

        ttk.Radiobutton(
            theme_frame,
            text=self.translator.translate("settings_tab", "theme_dark") or "Dark",
            variable=self.theme_var,
            value="dark"
        ).grid(row=0, column=2, padx=5, pady=5)

        ttk.Radiobutton(
            theme_frame,
            text=self.translator.translate("settings_tab", "theme_system") or "System",
            variable=self.theme_var,
            value="system"
        ).grid(row=0, column=3, padx=5, pady=5)

        # Theme note
        theme_note = ttk.Label(
            theme_frame,
            text=self.translator.translate("settings_tab", "theme_note") or "Theme changes will take effect after restart"
        )
        theme_note.grid(row=1, column=0, columnspan=4, padx=5, pady=5, sticky=tk.W)

        # Language settings
        language_frame = ttk.LabelFrame(self.frame, text=self.translator.translate("settings_tab", "language_settings"))
        language_frame.pack(fill=tk.X, padx=10, pady=10)

        language_label = ttk.Label(language_frame, text=self.translator.translate("settings_tab", "language_label"))
        language_label.grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)

        # Get language names for UI display
        language_names = self.translator.get_language_names()
        logging.info(f"Available language names: {language_names}")

        # Find current language name
        current_language = self.app.config.get('language', 'en')
        logging.info(f"Current language from config: {current_language}")

        # Get current language display name
        current_display_name = self.translator.language_map.get(current_language, current_language)
        logging.info(f"Current language display name: {current_display_name}")

        # Create and save reference to dropdown menu
        self.language_dropdown = ttk.Combobox(
            language_frame,
            values=language_names,
            state="readonly",
            width=20
        )

        # Try to find current language index
        try:
            if current_display_name in language_names:
                current_index = language_names.index(current_display_name)
            elif current_language in self.translator.available_languages:
                lang_idx = self.translator.available_languages.index(current_language)
                current_index = lang_idx if lang_idx < len(language_names) else 0
            else:
                current_index = 0

            logging.info(f"Setting dropdown to index {current_index} ({language_names[current_index] if language_names else 'N/A'})")
            self.language_dropdown.current(current_index)
        except Exception as e:
            logging.error(f"Error setting language dropdown: {str(e)}")
            if language_names:
                self.language_dropdown.set(language_names[0])

        self.language_dropdown.grid(row=0, column=1, padx=5, pady=5, sticky=tk.W)

        # Bind selection event to update language_var
        self.language_dropdown.bind("<<ComboboxSelected>>", self._on_language_changed)

        # Add note about language change requiring restart
        language_note = ttk.Label(
            language_frame,
            text=self.translator.translate("settings_tab", "language_note")
        )
        language_note.grid(row=1, column=0, columnspan=3, padx=5, pady=5, sticky=tk.W)

        # Other settings
        other_frame = ttk.LabelFrame(self.frame, text=self.translator.translate("settings_tab", "other_settings"))
        other_frame.pack(fill=tk.X, padx=10, pady=10)

        auto_open_check = ttk.Checkbutton(
            other_frame,
            text=self.translator.translate("settings_tab", "auto_open_check"),
            variable=self.auto_open_var
        )
        auto_open_check.grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)

        skip_existing_check = ttk.Checkbutton(
            other_frame,
            text=self.translator.translate("settings_tab", "skip_existing_check"),
            variable=self.skip_existing_var
        )
        skip_existing_check.grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)

        rename_with_desc_check = ttk.Checkbutton(
            other_frame,
            text=self.translator.translate("settings_tab", "rename_with_desc_check"),
            variable=self.rename_with_desc_var
        )
        rename_with_desc_check.grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)

        postprocess_check = ttk.Checkbutton(
            other_frame,
            text=self.translator.translate("settings_tab", "postprocess_enabled_check") or
                 "Enable video post-processing after download",
            variable=self.postprocess_enabled_var
        )
        postprocess_check.grid(row=3, column=0, padx=5, pady=5, sticky=tk.W)

        trim_seconds_label = ttk.Label(
            other_frame,
            text=self.translator.translate("settings_tab", "postprocess_trim_seconds_label") or
                 "Remove first N seconds:"
        )
        trim_seconds_label.grid(row=4, column=0, padx=5, pady=5, sticky=tk.W)

        trim_seconds_entry = ttk.Entry(other_frame, textvariable=self.postprocess_trim_seconds_var, width=12)
        trim_seconds_entry.grid(row=4, column=1, padx=5, pady=5, sticky=tk.W)

        mirror_video_check = ttk.Checkbutton(
            other_frame,
            text=self.translator.translate("settings_tab", "postprocess_mirror_video_check") or
                 "Mirror processed video",
            variable=self.postprocess_mirror_video_var
        )
        mirror_video_check.grid(row=5, column=0, padx=5, pady=5, sticky=tk.W)

        delete_original_check = ttk.Checkbutton(
            other_frame,
            text=self.translator.translate("settings_tab", "postprocess_delete_original_check") or
                 "Delete original file after successful processing",
            variable=self.postprocess_delete_original_var
        )
        delete_original_check.grid(row=6, column=0, padx=5, pady=5, sticky=tk.W)

        ffmpeg_label = ttk.Label(
            other_frame,
            text=self.translator.translate("settings_tab", "ffmpeg_path_label") or "FFmpeg Path:"
        )
        ffmpeg_label.grid(row=7, column=0, padx=5, pady=5, sticky=tk.W)

        ffmpeg_entry = ttk.Entry(other_frame, textvariable=self.ffmpeg_path_var, width=40)
        ffmpeg_entry.grid(row=7, column=1, padx=5, pady=5, sticky=tk.W + tk.E)

        ffmpeg_browse_button = ttk.Button(
            other_frame,
            text=self.translator.translate("settings_tab", "browse_button"),
            command=self._browse_ffmpeg
        )
        ffmpeg_browse_button.grid(row=7, column=2, padx=5, pady=5)

        ffmpeg_note = ttk.Label(
            other_frame,
            text=self.translator.translate("settings_tab", "ffmpeg_path_note") or
                 "Optional. Required for automatic video post-processing."
        )
        ffmpeg_note.grid(row=8, column=0, columnspan=3, padx=5, pady=5, sticky=tk.W)

        subtitle_cover_check = ttk.Checkbutton(
            other_frame,
            text=self._translate_or_default(
                "subtitle_cover_enabled_check",
                "下载完成后自动连接 SubtitleCover 继续处理"
            ),
            variable=self.subtitle_cover_enabled_var
        )
        subtitle_cover_check.grid(row=9, column=0, padx=5, pady=5, sticky=tk.W)

        subtitle_cover_label = ttk.Label(
            other_frame,
            text=self._translate_or_default(
                "subtitle_cover_workspace_label",
                "SubtitleCover 工作目录:"
            )
        )
        subtitle_cover_label.grid(row=10, column=0, padx=5, pady=5, sticky=tk.W)

        subtitle_cover_entry = ttk.Entry(other_frame, textvariable=self.subtitle_cover_workspace_var, width=40)
        subtitle_cover_entry.grid(row=10, column=1, padx=5, pady=5, sticky=tk.W + tk.E)

        subtitle_cover_browse_button = ttk.Button(
            other_frame,
            text=self.translator.translate("settings_tab", "browse_button"),
            command=self._browse_subtitle_cover_workspace
        )
        subtitle_cover_browse_button.grid(row=10, column=2, padx=5, pady=5)

        subtitle_cover_note = ttk.Label(
            other_frame,
            text=self._translate_or_default(
                "subtitle_cover_workspace_note",
                "填写 SubtitleCoverGUI_Rewrite 目录。下载器会调用其中的独立环境自动继续处理。"
            )
        )
        subtitle_cover_note.grid(row=11, column=0, columnspan=3, padx=5, pady=5, sticky=tk.W)

        # Save settings button
        save_settings_button = ttk.Button(
            other_frame,
            text=self.translator.translate("settings_tab", "save_settings_button"),
            command=self._save_settings
        )
        save_settings_button.grid(row=12, column=0, padx=5, pady=10)

        # Version check section - without auto-check option
        version_frame = ttk.LabelFrame(
            self.frame,
            text=self.translator.translate("settings_tab", "version_check") or "Version Check"
        )
        version_frame.pack(fill=tk.X, padx=10, pady=10)

        # Show current version
        current_version_text = self.translator.translate("settings_tab", "current_version") or "Current Version"
        current_version_label = ttk.Label(
            version_frame,
            text=f"{current_version_text}: {self.current_version}"
        )
        current_version_label.grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)

        # Add check for updates button
        check_update_text = self.translator.translate("settings_tab", "check_update_button") or "Check for Updates"
        check_update_button = ttk.Button(
            version_frame,
            text=check_update_text,
            command=self._check_for_updates,
        )
        check_update_button.grid(row=0, column=1, padx=5, pady=5)

        # About information - improved implementation with popup dialog
        about_frame = ttk.LabelFrame(self.frame, text=self.translator.translate("settings_tab", "about_frame"))
        about_frame.pack(fill=tk.X, padx=10, pady=10)

        # Create a compact about section with just summary and buttons
        header_frame = ttk.Frame(about_frame)
        header_frame.pack(fill=tk.X, padx=5, pady=5)

        app_title = ttk.Label(
            header_frame,
            text="TikHub Downloader",
            font=("Helvetica", 12, "bold")
        )
        app_title.pack(side=tk.LEFT, padx=(0, 10))

        version_label = ttk.Label(
            header_frame,
            text=f"v{self.current_version}"
        )
        version_label.pack(side=tk.LEFT)

        # Show Details button that opens a dialog instead of expanding in-place
        details_button = ttk.Button(
            header_frame,
            text=self.translator.translate("settings_tab", "show_details") or "Show Details",
            command=self._show_about_dialog,
            style="Accent.TButton"  # Use an accent style to make it stand out
        )
        details_button.pack(side=tk.RIGHT, padx=5)

        # Short about text for the main view
        brief_frame = ttk.Frame(about_frame)
        brief_frame.pack(fill=tk.X, padx=5, pady=5)

        brief_text = ttk.Label(
            brief_frame,
            text="A multi-functional downloader for TikTok, Douyin, and more platforms.",
            wraplength=500,
            justify=tk.LEFT
        )
        brief_text.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Footer with website link
        footer_frame = ttk.Frame(about_frame)
        footer_frame.pack(fill=tk.X, padx=5, pady=5)

        website_button = ttk.Button(
            footer_frame,
            text=self.translator.translate("settings_tab", "visit_website") or "Visit Website",
            command=lambda: webbrowser.open("https://www.tikhub.io")
        )
        website_button.pack(side=tk.RIGHT)

    def _paste_api_key(self):
        """Safe and simple API key paste function"""
        try:
            clipboard_content = self.frame.clipboard_get().strip()
            self.api_key_var.set(clipboard_content)
        except Exception as e:
            logging.error(f"Error pasting API key: {str(e)}")
            messagebox.showwarning(
                self.translator.translate("settings_tab", "warning"),
                self.translator.translate("settings_tab", "warning_clipboard_error")
            )

    def _clear_api_key(self):
        """Clear API key"""
        self.api_key_var.set("")

    def _reset_api_url(self):
        """Reset API URL to default"""
        self.api_url_var.set("https://api.tikhub.io")

    def _browse_ffmpeg(self):
        """Browse for ffmpeg executable"""
        file_path = filedialog.askopenfilename(
            title=self.translator.translate("settings_tab", "ffmpeg_select_title") or "Select ffmpeg executable",
            filetypes=[
                ("Executable", "*.exe"),
                ("All Files", "*.*")
            ]
        )
        if file_path:
            self.ffmpeg_path_var.set(file_path)

    def _browse_subtitle_cover_workspace(self):
        """Browse for SubtitleCover workspace"""
        folder_path = filedialog.askdirectory(
            initialdir=self.subtitle_cover_workspace_var.get() or os.getcwd()
        )
        if folder_path:
            self.subtitle_cover_workspace_var.set(folder_path)

    def _show_about_dialog(self):
        """Show About information in a separate dialog window"""
        # Get text based on selected language
        about_text = ABOUT_TEXT_EN if self.about_language.get() == "en" else ABOUT_TEXT_CN

        # Create a new dialog window
        about_dialog = tk.Toplevel(self.frame)
        about_dialog.title(self.translator.translate("settings_tab", "about_frame") or "About")
        about_dialog.geometry("1000x600")  # Larger size as requested
        about_dialog.minsize(800, 500)

        # Make dialog modal
        about_dialog.transient(self.frame)
        about_dialog.grab_set()

        # Center the window
        self._center_window(about_dialog)

        # Create a themed frame with padding
        frame = ttk.Frame(about_dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        # Create header with logo text and language toggle
        header_frame = ttk.Frame(frame)
        header_frame.pack(fill=tk.X, pady=(0, 15))

        # App title with standard text instead of emoji to avoid display issues
        app_title = ttk.Label(
            header_frame,
            text="TikHub Downloader",
            font=("Helvetica", 16, "bold")
        )
        app_title.pack(side=tk.LEFT)

        # Language toggle in the dialog
        language_frame = ttk.Frame(header_frame)
        language_frame.pack(side=tk.RIGHT)

        language_label = ttk.Label(language_frame, text="Language:")
        language_label.pack(side=tk.LEFT, padx=(0, 5))

        en_button = ttk.Radiobutton(
            language_frame,
            text="English",
            variable=self.about_language,
            value="en",
            command=lambda: self._update_about_dialog_text(about_text_widget)
        )
        en_button.pack(side=tk.LEFT, padx=2)

        cn_button = ttk.Radiobutton(
            language_frame,
            text="中文",
            variable=self.about_language,
            value="cn",
            command=lambda: self._update_about_dialog_text(about_text_widget)
        )
        cn_button.pack(side=tk.LEFT, padx=2)

        # Add a separator
        ttk.Separator(frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        # Create a scrollable text widget with better styling
        text_frame = ttk.Frame(frame)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        about_text_widget = tk.Text(
            text_frame,
            wrap=tk.WORD,
            padx=10,
            pady=10,
            font=("Helvetica", 11),  # Use a common font
            highlightthickness=0,
            relief=tk.FLAT
        )

        scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=about_text_widget.yview)
        about_text_widget.configure(yscrollcommand=scroll.set)

        about_text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Insert about text
        self._update_about_dialog_text(about_text_widget)

        # Add a version and copyright info at the bottom
        info_frame = ttk.Frame(frame)
        info_frame.pack(fill=tk.X, pady=10)

        version_text = f"Version: {self.current_version}"
        version_label = ttk.Label(info_frame, text=version_text)
        version_label.pack(side=tk.LEFT)

        copyright_label = ttk.Label(info_frame, text="Copyright (c) 2025 TikHub.io")
        copyright_label.pack(side=tk.RIGHT)

        # Add buttons at the bottom
        button_frame = ttk.Frame(frame)
        button_frame.pack(fill=tk.X, pady=10)

        website_button = ttk.Button(
            button_frame,
            text=self.translator.translate("settings_tab", "visit_website") or "Visit Website",
            command=lambda: webbrowser.open("https://www.tikhub.io")
        )
        website_button.pack(side=tk.LEFT, padx=5)

        docs_button = ttk.Button(
            button_frame,
            text="API Documentation",
            command=lambda: webbrowser.open("https://docs.tikhub.io")
        )
        docs_button.pack(side=tk.LEFT, padx=5)

        close_button = ttk.Button(
            button_frame,
            text=self.translator.translate("settings_tab", "close_button") or "Close",
            command=about_dialog.destroy
        )
        close_button.pack(side=tk.RIGHT, padx=5)

        # Wait for the dialog to be closed
        self.frame.wait_window(about_dialog)

    def _update_about_dialog_text(self, text_widget):
        """Update the about dialog text based on selected language"""
        # Get text based on selected language
        about_text = ABOUT_TEXT_EN if self.about_language.get() == "en" else ABOUT_TEXT_CN

        # Clear and insert the text
        text_widget.config(state=tk.NORMAL)
        text_widget.delete(1.0, tk.END)
        text_widget.insert(tk.END, about_text)

        # Apply text formatting
        text_widget.tag_configure("bold", font=("Helvetica", 11, "bold"))

        # Find and format all bold text between ** markers
        start = "1.0"
        while True:
            bold_start = text_widget.search(r"\*\*", start, tk.END, regexp=True)
            if not bold_start:
                break

            bold_end = text_widget.search(r"\*\*", f"{bold_start}+2c", tk.END, regexp=True)
            if not bold_end:
                break

            text_widget.tag_add("bold", bold_start, f"{bold_end}+2c")
            text_widget.delete(bold_end, f"{bold_end}+2c")
            text_widget.delete(bold_start, f"{bold_start}+2c")

            # Adjust start position for next search
            start = bold_end

        # Make read-only after editing
        text_widget.config(state=tk.DISABLED)

    def _check_for_updates(self):
        """Check for application updates - using background thread"""
        try:
            # Show checking dialog
            checking_title = self.translator.translate("settings_tab", "checking_update") or "Checking for Updates"
            checking_message = self.translator.translate("settings_tab", "checking_for_updates") or "Checking for updates, please wait..."

            progress_window = tk.Toplevel(self.frame)
            progress_window.title(checking_title)
            progress_window.geometry("300x100")
            self._center_window(progress_window)

            # Make dialog modal
            progress_window.transient(self.frame)
            progress_window.grab_set()

            progress_label = ttk.Label(
                progress_window,
                text=checking_message,
                padding=10
            )
            progress_label.pack(pady=5)

            progress_bar = ttk.Progressbar(progress_window, mode="indeterminate", length=200)
            progress_bar.pack(pady=10)
            progress_bar.start()

            # Add a cancel button
            cancel_button = ttk.Button(
                progress_window,
                text=self.translator.translate("settings_tab", "close_button") or "Cancel",
                command=progress_window.destroy
            )
            cancel_button.pack(pady=5)

            # Update UI
            self.frame.update_idletasks()

            # Create a function to run in background thread
            def check_update_thread():
                result = None
                error = None

                try:
                    update_check_url = self.update_check_url
                    version_response = httpx.get(update_check_url, timeout=5.0)
                    version_response.raise_for_status()
                    result = version_response.json()
                except Exception as e:
                    error = e
                    logging.error(f"Error checking for updates: {str(e)}")

                # Update UI in the main thread
                self.frame.after(0, lambda: self._process_update_result(progress_window, result, error))

            # Start the background thread
            threading.Thread(target=check_update_thread, daemon=True).start()

        except Exception as e:
            error_title = self.translator.translate("settings_tab", "error") or "Error"
            error_message = self.translator.translate("settings_tab", "update_check_error") or "Error checking for updates: {error}"

            logging.error(f"Error setting up update check: {str(e)}")
            messagebox.showerror(
                error_title,
                error_message.format(error=str(e))
            )

    def _process_update_result(self, progress_window, result, error):
        """Process update check results"""
        # Close progress window if it still exists
        if progress_window and progress_window.winfo_exists():
            progress_window.destroy()

        if error:
            error_title = self.translator.translate("settings_tab", "error") or "Error"
            error_message = self.translator.translate("settings_tab", "update_check_error") or "Error checking for updates: {error}"

            messagebox.showerror(
                error_title,
                error_message.format(error=str(error))
            )
            return

        try:
            latest_version = result.get('latest_version', self.current_version)

            # Check version
            if self._compare_versions(latest_version, self.current_version) > 0:
                self._show_update_available(result)
            else:
                self._show_no_update_needed()

        except Exception as e:
            error_title = self.translator.translate("settings_tab", "error") or "Error"
            error_message = self.translator.translate("settings_tab", "update_check_error") or "Error processing update check results: {error}"

            logging.error(f"Error processing update check: {str(e)}")
            messagebox.showerror(
                error_title,
                error_message.format(error=str(e))
            )

    def _compare_versions(self, ver1, ver2):
        """Compare version numbers

        Args:
            ver1: First version number
            ver2: Second version number

        Returns:
            int: 1 if ver1 > ver2, -1 if ver1 < ver2, 0 if equal
        """
        def normalize(v):
            return [int(x) for x in re.sub(r'(\.0+)*$', '', v).split(".")]

        ver1_parts = normalize(ver1)
        ver2_parts = normalize(ver2)

        for v1, v2 in zip(ver1_parts, ver2_parts):
            if v1 > v2:
                return 1
            if v1 < v2:
                return -1

        if len(ver1_parts) > len(ver2_parts):
            return 1
        elif len(ver1_parts) < len(ver2_parts):
            return -1
        else:
            return 0

    def _show_update_available(self, version_data):
        """Show update available dialog

        Args:
            version_data: Version information data
        """
        latest_version = version_data.get('latest_version', self.current_version)
        download_url = version_data.get('download_url', '')

        # Get translated text
        update_title = self.translator.translate("settings_tab", "update_available") or "Update Available"
        update_header = self.translator.translate("settings_tab", "update_available_title") or "New Version Available!"
        update_message_template = self.translator.translate(
            "settings_tab", "update_message"
        ) or "Current version: {current_version}\nLatest version: {latest_version}\n\nIt is recommended to upgrade to the latest version for new features and fixes."
        download_button_text = self.translator.translate("settings_tab", "download_update") or "Download Update"
        close_button_text = self.translator.translate("settings_tab", "close_button") or "Close"

        update_window = tk.Toplevel(self.frame)
        update_window.title(update_title)
        update_window.geometry("800x400")
        self._center_window(update_window)

        frame = ttk.Frame(update_window, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        # Title
        header = ttk.Label(
            frame,
            text=update_header,
            font=("Helvetica", 14, "bold")
        )
        header.pack(pady=(0, 15))

        # Version information
        info_text = update_message_template.format(
            current_version=self.current_version,
            latest_version=latest_version
        )
        info_label = ttk.Label(frame, text=info_text, wraplength=350, justify=tk.LEFT)
        info_label.pack(pady=10)

        # Buttons
        button_frame = ttk.Frame(frame)
        button_frame.pack(pady=15)

        download_button = ttk.Button(
            button_frame,
            text=download_button_text,
            command=lambda: webbrowser.open(download_url)
        )
        download_button.pack(side=tk.LEFT, padx=5)

        close_button = ttk.Button(
            button_frame,
            text=close_button_text,
            command=update_window.destroy
        )
        close_button.pack(side=tk.LEFT, padx=5)

        update_window.transient(self.frame)
        update_window.grab_set()
        self.frame.wait_window(update_window)

    def _show_no_update_needed(self):
        """Show no update needed dialog"""
        title = self.translator.translate("settings_tab", "version_check") or "Version Check"
        message_template = self.translator.translate(
            "settings_tab", "no_update_needed"
        ) or "Current version ({version}) is up to date."

        messagebox.showinfo(
            title,
            message_template.format(version=self.current_version)
        )

    def _on_language_changed(self, event):
        """Handle language selection change"""
        # Get the language name from combobox
        language_name = event.widget.get()
        logging.info(f"Language selection changed to: {language_name}")

        # Convert to language code
        language_code = self.translator.get_language_code_from_name(language_name)
        logging.info(f"Converted to language code: {language_code}")

        # Update the language variable
        self.language_var.set(language_code)
        logging.info(f"Updated language_var to: {language_code}")

    def _toggle_show_password(self, entry_widget):
        """Toggle password display/hiding"""
        if entry_widget.cget('show') == '*':
            entry_widget.config(show='')
        else:
            entry_widget.config(show='*')

    def _browse_folder(self):
        """Browse for download folder"""
        folder_path = filedialog.askdirectory(initialdir=self.app.download_path)
        if folder_path:
            self.path_var.set(folder_path)

    def _save_api_key(self):
        """Save API key and URL"""
        api_key = self.api_key_var.get().strip()
        api_url = self.api_url_var.get().strip()

        if not api_key:
            messagebox.showwarning(
                self.translator.translate("settings_tab", "warning"),
                self.translator.translate("settings_tab", "warning_empty_api_key")
            )
            return

        # Save API URL
        if api_url and api_url != self.app.config.get('api_base_url'):
            self.app.config.set('api_base_url', api_url)
            self.app.config.save()

            # If API URL changed, we should reinitialize the client
            # Note: This would require modifying the client initialization code elsewhere

        # Update API key
        api_key_check = self.app.client.update_api_key(api_key)
        if api_key_check and api_key_check.get('code') == 200:
            # Save to config
            self.app.config.set('api_key', api_key)
            self.app.config.save()

            # Create a custom dialog with formatted info instead of a standard messagebox
            self._show_api_key_info(api_key_check, api_key)
        else:
            self._show_api_key_error(api_key_check)

    def _show_api_key_info(self, api_key_data, api_key):
        """Display API key info in a nicely formatted dialog"""
        # Extract user data
        user_data = api_key_data.get('user_data', {})
        api_key_info = api_key_data.get('api_key_data', {})

        # Create a custom top-level window
        info_window = tk.Toplevel(self.frame)
        info_window.title(self.translator.translate("settings_tab", "api_key_info_title"))
        info_window.geometry("600x1200")
        info_window.minsize(600, 1200)  # Set minimum size

        # Center the window
        self._center_window(info_window)

        # Add a themed frame
        frame = ttk.Frame(info_window, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        # Add a header
        header = ttk.Label(
            frame,
            text=self.translator.translate("settings_tab", "api_key_success"),
            font=("Helvetica", 14, "bold")
        )
        header.pack(pady=(0, 15))

        # Create sections
        account_frame = ttk.LabelFrame(
            frame,
            text=self.translator.translate("settings_tab", "account_info"),
            padding=10
        )
        account_frame.pack(fill=tk.X, pady=5)

        # Account info
        email_label = ttk.Label(
            account_frame,
            text=self.translator.translate("settings_tab", "email_label"),
            width=15,
            anchor=tk.W
        )
        email_label.grid(row=0, column=0, sticky=tk.W, pady=2)
        email_value = ttk.Label(account_frame, text=user_data.get('email', self.translator.translate("settings_tab", "unknown")))
        email_value.grid(row=0, column=1, sticky=tk.W)

        balance_label = ttk.Label(
            account_frame,
            text=self.translator.translate("settings_tab", "balance_label"),
            width=15,
            anchor=tk.W
        )
        balance_label.grid(row=1, column=0, sticky=tk.W, pady=2)
        balance_value = ttk.Label(account_frame, text=user_data.get('balance', self.translator.translate("settings_tab", "unknown")))
        balance_value.grid(row=1, column=1, sticky=tk.W)

        credit_label = ttk.Label(
            account_frame,
            text=self.translator.translate("settings_tab", "credit_label"),
            width=15,
            anchor=tk.W
        )
        credit_label.grid(row=2, column=0, sticky=tk.W, pady=2)
        credit_value = ttk.Label(account_frame, text=user_data.get('free_credit', self.translator.translate("settings_tab", "unknown")))
        credit_value.grid(row=2, column=1, sticky=tk.W)

        # Make columns expandable
        account_frame.columnconfigure(1, weight=1)

        # Status indicators with colors
        status_frame = ttk.LabelFrame(
            frame,
            text=self.translator.translate("settings_tab", "account_status"),
            padding=10
        )
        status_frame.pack(fill=tk.X, pady=5)

        # Function to create status indicator
        def add_status_indicator(parent, row, label, value):
            label_widget = ttk.Label(
                parent,
                text=f"{self.translator.translate('settings_tab', label)}:",
                width=15,
                anchor=tk.W
            )
            label_widget.grid(row=row, column=0, sticky=tk.W, pady=2)

            # Use checkmark/cross symbols and colors
            if value:
                indicator = "✓"
                color = "green"
            else:
                indicator = "✗"
                color = "red"

            value_widget = ttk.Label(parent, text=f"{indicator} {str(value)}")
            value_widget.grid(row=row, column=1, sticky=tk.W)

            # Try to apply color (may require custom style)
            try:
                value_widget.configure(foreground=color)
            except:
                pass

        add_status_indicator(status_frame, 0, "email_verified",
                             user_data.get('email_verified', False))
        add_status_indicator(status_frame, 1, "account_disabled",
                             not user_data.get('account_disabled', True))  # Invert for positive meaning
        add_status_indicator(status_frame, 2, "is_active",
                             user_data.get('is_active', False))

        # Make columns expandable
        status_frame.columnconfigure(1, weight=1)

        # API Key details
        key_frame = ttk.LabelFrame(
            frame,
            text=self.translator.translate("settings_tab", "api_key_details"),
            padding=10
        )
        key_frame.pack(fill=tk.X, pady=5)

        created_label = ttk.Label(
            key_frame,
            text=self.translator.translate("settings_tab", "created_label"),
            width=15,
            anchor=tk.W
        )
        created_label.grid(row=0, column=0, sticky=tk.W, pady=2)
        created_value = ttk.Label(key_frame, text=api_key_info.get('created_at', self.translator.translate("settings_tab", "unknown")))
        created_value.grid(row=0, column=1, sticky=tk.W)

        expires_label = ttk.Label(
            key_frame,
            text=self.translator.translate("settings_tab", "expires_label"),
            width=15,
            anchor=tk.W
        )
        expires_label.grid(row=1, column=0, sticky=tk.W, pady=2)
        expires_value = ttk.Label(
            key_frame,
            text=api_key_info.get('expires_at', self.translator.translate("settings_tab", "not_set")) or
                 self.translator.translate("settings_tab", "never")
        )
        expires_value.grid(row=1, column=1, sticky=tk.W)

        # Add API key
        key_label = ttk.Label(
            key_frame,
            text=self.translator.translate("settings_tab", "api_key_label"),
            width=15,
            anchor=tk.W
        )
        key_label.grid(row=2, column=0, sticky=tk.W, pady=2)

        if len(api_key) > 8:
            masked_key = api_key[:4] + "****" + api_key[-4:]
        else:
            masked_key = api_key

        key_value = ttk.Label(key_frame, text=masked_key)
        key_value.grid(row=2, column=1, sticky=tk.W)

        # Make columns expandable
        key_frame.columnconfigure(1, weight=1)

        # Add API scopes
        scopes_frame = ttk.LabelFrame(
            frame,
            text=self.translator.translate("settings_tab", "api_scopes"),
            padding=10
        )
        scopes_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        # Create a scrollable text widget for scopes
        scopes_text = tk.Text(scopes_frame, height=5, wrap=tk.WORD)
        scopes_scroll = ttk.Scrollbar(scopes_frame, orient=tk.VERTICAL, command=scopes_text.yview)
        scopes_text.configure(yscrollcommand=scopes_scroll.set)

        scopes_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scopes_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Add scopes to text widget
        scopes = api_key_info.get('api_key_scopes', [])
        scopes_text.insert(tk.END, "\n".join(scopes))
        scopes_text.config(state=tk.DISABLED)  # Make read-only

        # Close button
        close_button = ttk.Button(
            frame,
            text=self.translator.translate("settings_tab", "close_button"),
            command=info_window.destroy
        )
        close_button.pack(pady=15)

        # Make the window modal
        info_window.transient(self.frame)
        info_window.grab_set()
        self.frame.wait_window(info_window)

    def _show_api_key_error(self, error_data):
        """Display API key error in a formatted dialog"""
        # Get error details
        detail = error_data.get('detail', {})
        code = detail.get('code', self.translator.translate("settings_tab", "unknown"))
        message = detail.get('message', self.translator.translate("settings_tab", "unknown_error"))
        support = detail.get('support', self.translator.translate("settings_tab", "unknown"))
        timestamp = detail.get('time', self.translator.translate("settings_tab", "unknown"))

        # Create a custom error dialog
        error_window = tk.Toplevel(self.frame)
        error_window.title(self.translator.translate("settings_tab", "api_key_error"))
        error_window.geometry("800x800")
        error_window.minsize(800, 800)  # Set minimum size

        # Center the window
        self._center_window(error_window)

        # Add themed frame with padding
        frame = ttk.Frame(error_window, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        # Error header with icon
        header_frame = ttk.Frame(frame)
        header_frame.pack(fill=tk.X, pady=(0, 15))

        # Using Label for the error icon (could use an image)
        icon_label = ttk.Label(header_frame, text="⚠", font=("Helvetica", 24))
        icon_label.pack(side=tk.LEFT, padx=(0, 10))

        header = ttk.Label(
            header_frame,
            text=self.translator.translate("settings_tab", "api_key_validation_failed"),
            font=("Helvetica", 14, "bold")
        )
        header.pack(side=tk.LEFT)

        # Error details
        error_frame = ttk.LabelFrame(
            frame,
            text=self.translator.translate("settings_tab", "error_details"),
            padding=10
        )
        error_frame.pack(fill=tk.BOTH, expand=True)

        code_label = ttk.Label(
            error_frame,
            text=self.translator.translate("settings_tab", "error_code"),
            width=10,
            anchor=tk.W
        )
        code_label.grid(row=0, column=0, sticky=tk.W, pady=5)
        code_value = ttk.Label(error_frame, text=code)
        code_value.grid(row=0, column=1, sticky=tk.W)

        # Message in a scrolled text area in case it's long
        msg_label = ttk.Label(
            error_frame,
            text=self.translator.translate("settings_tab", "message_label"),
            width=10,
            anchor=tk.NW
        )
        msg_label.grid(row=1, column=0, sticky=tk.NW, pady=5)

        msg_text = tk.Text(error_frame, height=6, width=40, wrap=tk.WORD)
        msg_text.grid(row=1, column=1, sticky=tk.EW)
        msg_text.insert(tk.END, message)
        msg_text.config(state=tk.DISABLED)

        # Add scrollbar to message text
        msg_scroll = ttk.Scrollbar(error_frame, orient=tk.VERTICAL, command=msg_text.yview)
        msg_scroll.grid(row=1, column=2, sticky=tk.NS)
        msg_text.config(yscrollcommand=msg_scroll.set)

        support_label = ttk.Label(
            error_frame,
            text=self.translator.translate("settings_tab", "support_label"),
            width=10,
            anchor=tk.W
        )
        support_label.grid(row=2, column=0, sticky=tk.W, pady=5)
        support_value = ttk.Label(error_frame, text=support)
        support_value.grid(row=2, column=1, sticky=tk.W)

        time_label = ttk.Label(
            error_frame,
            text=self.translator.translate("settings_tab", "time_label"),
            width=10,
            anchor=tk.W
        )
        time_label.grid(row=3, column=0, sticky=tk.W, pady=5)
        time_value = ttk.Label(error_frame, text=timestamp)
        time_value.grid(row=3, column=1, sticky=tk.W)

        # Configure grid column weights
        error_frame.columnconfigure(1, weight=1)

        # Close button
        close_button = ttk.Button(
            frame,
            text=self.translator.translate("settings_tab", "close_button"),
            command=error_window.destroy
        )
        close_button.pack(pady=10)

        # Make dialog modal
        error_window.transient(self.frame)
        error_window.grab_set()
        self.frame.wait_window(error_window)

    def _center_window(self, window):
        """Center the given window on screen

        Args:
            window: The window to center
        """
        window.update_idletasks()
        width = window.winfo_width()
        height = window.winfo_height()
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()

        x = (screen_width - width) // 2
        y = (screen_height - height) // 2

        window.geometry(f"{width}x{height}+{x}+{y}")

    def _translate_or_default(self, key, default_text):
        text = self.translator.translate("settings_tab", key)
        return default_text if text == key else text

    def _guess_subtitle_cover_workspace(self):
        candidates = [
            os.path.abspath(os.path.join(os.getcwd(), "..", "SubtitleCoverGUI_Rewrite")),
            os.path.abspath(os.path.join(os.getcwd(), "SubtitleCoverGUI_Rewrite")),
            os.path.abspath(os.path.join(os.path.dirname(os.getcwd()), "SubtitleCoverGUI_Rewrite")),
        ]
        for candidate in candidates:
            if os.path.isfile(os.path.join(candidate, "app.py")):
                return candidate
        return ""

    def _save_settings(self):
        """Save all settings"""
        try:
            # Get path
            download_path = self.path_var.get().strip()
            if download_path and os.path.exists(download_path):
                self.app.download_path = download_path
                self.app.config.set('download_path', download_path)

            # Save language setting
            selected_language = self.language_var.get()
            current_language = self.app.config.get('language', 'en')

            logging.info(
                f"Saving settings - Selected language: {selected_language}, Current language: {current_language}")

            if selected_language != current_language:
                logging.info(f"Language changed from {current_language} to {selected_language}")
                self.app.config.set('language', selected_language)
                # Show message about restart if language changed
                messagebox.showinfo(
                    self.translator.translate("settings_tab", "language_changed_title"),
                    self.translator.translate("settings_tab", "language_changed_message")
                )

            # Save theme setting
            selected_theme = self.theme_var.get()
            current_theme = self.app.config.get('theme', 'system')

            if selected_theme != current_theme:
                logging.info(f"Theme changed from {current_theme} to {selected_theme}")

                # 保存主题设置
                self.app.config.set('theme', selected_theme)

                # 尝试动态应用主题 (如果app实现了apply_theme方法)
                try:
                    if hasattr(self.app, 'apply_theme') and callable(self.app.apply_theme):
                        self.app.apply_theme(selected_theme)
                    else:
                        # 如果没有动态应用功能，显示需要重启的消息
                        messagebox.showinfo(
                            self.translator.translate("settings_tab", "theme_changed_title") or "Theme Changed",
                            self.translator.translate("settings_tab", "theme_changed_message") or
                            "Theme changes will take effect after restarting the application."
                        )
                except Exception as e:
                    logging.error(f"Error applying theme: {str(e)}")
                    messagebox.showinfo(
                        self.translator.translate("settings_tab", "theme_changed_title") or "Theme Changed",
                        self.translator.translate("settings_tab", "theme_changed_message") or
                        "Theme changes will take effect after restarting the application."
                    )

            trim_seconds_raw = self.postprocess_trim_seconds_var.get().strip()
            trim_seconds = float(trim_seconds_raw) if trim_seconds_raw else 1.5
            if trim_seconds < 0:
                raise ValueError("Trim seconds must be 0 or greater")

            subtitle_cover_workspace = self.subtitle_cover_workspace_var.get().strip()
            if self.subtitle_cover_enabled_var.get():
                if not subtitle_cover_workspace:
                    raise ValueError("请填写 SubtitleCoverGUI_Rewrite 目录")
                runner_path = os.path.join(subtitle_cover_workspace, "automation_runner.py")
                if not os.path.isfile(runner_path):
                    raise ValueError("所选目录中缺少 automation_runner.py，请确认选择的是 SubtitleCoverGUI_Rewrite")

            # Save other settings
            self.app.config.set('auto_open_folder', self.auto_open_var.get())
            self.app.config.set('skip_existing', self.skip_existing_var.get())
            self.app.config.set('rename_with_desc', self.rename_with_desc_var.get())
            self.app.config.set('postprocess_enabled', self.postprocess_enabled_var.get())
            self.app.config.set('postprocess_mirror_trim', self.postprocess_enabled_var.get())
            self.app.config.set('postprocess_trim_start_seconds', trim_seconds)
            self.app.config.set('postprocess_mirror_video', self.postprocess_mirror_video_var.get())
            self.app.config.set('postprocess_delete_original', self.postprocess_delete_original_var.get())
            self.app.config.set('ffmpeg_path', self.ffmpeg_path_var.get().strip())
            self.app.config.set('subtitle_cover_enabled', self.subtitle_cover_enabled_var.get())
            self.app.config.set('subtitle_cover_workspace', subtitle_cover_workspace)

            # Save config
            self.app.config.save()
            logging.info("Settings saved successfully")

            messagebox.showinfo(
                self.translator.translate("settings_tab", "success"),
                self.translator.translate("settings_tab", "settings_saved")
            )
        except Exception as e:
            logging.error(f"Error saving settings: {str(e)}")
            messagebox.showerror(
                self.translator.translate("settings_tab", "error"),
                self.translator.translate("settings_tab", "save_settings_error").format(error=str(e))
            )

    def get_settings(self):
        """Get current settings

        Returns:
            dict: Settings dictionary
        """
        return {
            'download_path': self.path_var.get(),
            'auto_open_folder': self.auto_open_var.get(),
            'skip_existing': self.skip_existing_var.get(),
            'rename_with_desc': self.rename_with_desc_var.get(),
            'postprocess_enabled': self.postprocess_enabled_var.get(),
            'postprocess_trim_start_seconds': float(self.postprocess_trim_seconds_var.get().strip() or "1.5"),
            'postprocess_mirror_video': self.postprocess_mirror_video_var.get(),
            'postprocess_delete_original': self.postprocess_delete_original_var.get(),
            'ffmpeg_path': self.ffmpeg_path_var.get().strip(),
            'subtitle_cover_enabled': self.subtitle_cover_enabled_var.get(),
            'subtitle_cover_workspace': self.subtitle_cover_workspace_var.get().strip(),
            'subtitle_cover_detect_fps': float(self.app.config.get('subtitle_cover_detect_fps', 1.5)),
            'subtitle_cover_min_score': float(self.app.config.get('subtitle_cover_min_score', 0.38)),
            'subtitle_cover_mode': self.app.config.get('subtitle_cover_mode', 'blur'),
            'subtitle_cover_blur_kernel': int(self.app.config.get('subtitle_cover_blur_kernel', 41)),
            'subtitle_cover_white_alpha': float(self.app.config.get('subtitle_cover_white_alpha', 0.88)),
            'subtitle_ai_workspace': self.app.config.get('subtitle_ai_workspace', ''),
            'subtitle_ai_auto_detect': self.app.config.get('subtitle_ai_auto_detect', True),
            'subtitle_ai_require_cuda': self.app.config.get('subtitle_ai_require_cuda', True),
            'subtitle_ai_quality': int(self.app.config.get('subtitle_ai_quality', 18)),
            'language': self.language_var.get(),
            'theme': self.theme_var.get()
        }

"""
Main application class for TikHub Downloader
"""

import locale
import os
import platform
import re
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox

import ttkbootstrap as ttk

from downloader.apis.api_client import MainAPIClient
from downloader.config import Config
from downloader.constants import APP_TITLE, DEFAULT_WINDOW_SIZE, APP_VERSION, APP_UPDATE_DATE, APP_COPYRIGHT
from downloader.locales.translate import Translator
from downloader.utils.theme_utils import get_theme_name


class TikHubDownloaderApp:
    """Main application controller for TikHub Downloader"""

    def __init__(self, root):
        """Initialize the application

        Args:
            root: The tkinter root window
        """
        self.root = root
        self.root.title(APP_TITLE)

        # 设置应用初始时为最大化窗口
        if platform.system() == "Windows":
            self.root.state('zoomed')  # 在Windows上使用'zoomed'
        else:
            # 在Linux/Mac等系统上使用属性设置
            self.root.attributes('-fullscreen', True)

        # self.root.geometry(DEFAULT_WINDOW_SIZE)

        self.root.configure(padx=20, pady=20)

        # Get the window dimensions
        self.width = int(DEFAULT_WINDOW_SIZE.split('x')[0])
        self.height = int(DEFAULT_WINDOW_SIZE.split('x')[1])

        # Set minimum window size
        self.root.minsize(width=self.width, height=self.height)

        # Load configuration
        self.config = Config()

        # Check if this is the first run and detect system language
        self._detect_and_set_language()

        # Initialize translator with language from config
        language = self.config.get('language', 'en')
        self.translator = Translator(language)

        # Initialize the client with API key from config
        self.client = MainAPIClient(
            api_key=self.config.get('api_key'),
            base_url=self.config.get('api_base_url'),
            proxy=self.config.get('proxy', None)
        )

        # Initialize the download path
        self.download_path = self.config.get('download_path', os.path.join(os.getcwd(), "downloads"))
        os.makedirs(self.download_path, exist_ok=True)

        # Import main window here to avoid circular imports
        from downloader.ui.main_window import MainWindow

        # Initialize the main window
        self.main_window = MainWindow(self.root, self)
        self.settings_tab = self.main_window.settings_tab

        # Center the window
        self._center_window()

        # Check if this is the first run (no API key configured)
        if not self.client.is_configured:
            self._show_welcome_dialog()

    def _detect_and_set_language(self):
        """
        Detect system language and set application language accordingly
        for the first run
        """
        # 修复: 使用get方法检查语言是否已配置，而不是直接访问data属性
        current_language = self.config.get('language', None)

        # 只有当未设置语言时才检测系统语言并设置默认语言
        if current_language is None:
            try:
                # Get system locale
                system_locale, _ = locale.getdefaultlocale()

                # Default to English
                default_language = 'en'

                if system_locale:
                    # Check if Chinese
                    if re.match(r'zh_', system_locale):
                        default_language = 'zh'

                # Set and save language
                self.config.set('language', default_language)
                self.config.save()

            except Exception as e:
                # If any error occurs, fallback to English
                print(f"Error detecting system language: {str(e)}")
                self.config.set('language', 'en')
                self.config.save()

    def run(self):
        """Run the application main loop"""
        # Enforce window size and position
        self._center_window()
        # Start the Tkinter main loop
        self.root.mainloop()

    def _center_window(self):
        """Center the window on the screen and enforce window size"""
        # First set the window size with geometry
        self.root.geometry(DEFAULT_WINDOW_SIZE)

        # Update idletasks to ensure geometry takes effect
        self.root.update_idletasks()

        # Get the window dimensions
        width = self.width
        height = self.height

        # Calculate position for center of screen
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()

        x = (screen_width - width) // 2
        y = (screen_height - height) // 2

        # y coordinate should be little higher, to make the bottom part visible(Taskbar will hide the bottom part)
        y = y - 50

        # Set window geometry with position
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _show_welcome_dialog(self):
        """Display a welcome dialog for first-time users with guidance and setup instructions"""
        welcome_window = tk.Toplevel(self.root)
        # Use translator for UI text
        welcome_window.title(self.translator.translate("app", "welcome_title"))
        welcome_window.geometry("1200x1000")
        welcome_window.minsize(1200, 1000)

        # Center the dialog
        welcome_window.transient(self.root)
        welcome_window.grab_set()
        welcome_window.update_idletasks()

        width = welcome_window.winfo_width()
        height = welcome_window.winfo_height()
        screen_width = welcome_window.winfo_screenwidth()
        screen_height = welcome_window.winfo_screenheight()

        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        welcome_window.geometry(f"{width}x{height}+{x}+{y}")

        # Create the main frame with padding
        main_frame = ttk.Frame(welcome_window, padding=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Welcome header
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill=tk.X, pady=(0, 15))

        # Welcome title
        title = ttk.Label(
            header_frame,
            text=self.translator.translate("app", "welcome_header"),
            font=("Helvetica", 16, "bold")
        )
        title.pack(pady=(0, 5))

        subtitle = ttk.Label(
            header_frame,
            text=self.translator.translate("app", "welcome_subtitle"),
            wraplength=600,
            justify=tk.CENTER
        )
        subtitle.pack()

        # Separator
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        # Content area with notebook tabs
        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill=tk.BOTH, expand=True, pady=10)

        # Setup tab
        setup_frame = ttk.Frame(notebook, padding=10)
        notebook.add(setup_frame, text=self.translator.translate("app", "setup_tab"))

        setup_label = ttk.Label(
            setup_frame,
            text=self.translator.translate("app", "getting_started"),
            font=("Helvetica", 12, "bold")
        )
        setup_label.pack(anchor=tk.W, pady=(0, 10))

        setup_text = self.translator.translate("app", "setup_instructions")

        setup_info = ttk.Label(
            setup_frame,
            text=setup_text,
            wraplength=600,
            justify=tk.LEFT
        )
        setup_info.pack(fill=tk.X, pady=5)

        # Add a button to directly open Settings tab
        def go_to_settings():
            welcome_window.destroy()
            self.main_window.tab_control.select(3)  # Index 3 is the Settings tab

        settings_button = ttk.Button(
            setup_frame,
            text=self.translator.translate("app", "go_to_settings"),
            command=go_to_settings
        )
        settings_button.pack(pady=10)

        ttk.Separator(setup_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        # Website button
        website_label = ttk.Label(
            setup_frame,
            text=self.translator.translate("app", "no_account_yet"),
            font=("Helvetica", 11)
        )
        website_label.pack(pady=(10, 5))

        website_button = ttk.Button(
            setup_frame,
            text=self.translator.translate("app", "create_account"),
            command=lambda: webbrowser.open("https://user.tikhub.io/users/api_keys")
        )
        website_button.pack()

        # Features tab
        features_frame = ttk.Frame(notebook, padding=10)
        notebook.add(features_frame, text=self.translator.translate("app", "features_tab"))

        features_label = ttk.Label(
            features_frame,
            text=self.translator.translate("app", "key_features"),
            font=("Helvetica", 12, "bold")
        )
        features_label.pack(anchor=tk.W, pady=(0, 10))

        # Keep the original features list structure but use translation keys
        features = [
            ("feature1_title", "feature1_desc"),
            ("feature2_title", "feature2_desc"),
            ("feature3_title", "feature3_desc"),
            ("feature4_title", "feature4_desc"),
            ("feature5_title", "feature5_desc")
        ]

        for title_key, desc_key in features:
            feature_frame = ttk.Frame(features_frame)
            feature_frame.pack(fill=tk.X, pady=5)

            feature_title = ttk.Label(
                feature_frame,
                text=f"• {self.translator.translate('app', title_key)}:",
                width=15,
                font=("Helvetica", 10, "bold")
            )
            feature_title.pack(side=tk.LEFT, anchor=tk.NW)

            feature_desc = ttk.Label(
                feature_frame,
                text=self.translator.translate("app", desc_key),
                wraplength=450
            )
            feature_desc.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Tips tab
        tips_frame = ttk.Frame(notebook, padding=10)
        notebook.add(tips_frame, text=self.translator.translate("app", "tips_tab"))

        tips_label = ttk.Label(
            tips_frame,
            text=self.translator.translate("app", "tips_header"),
            font=("Helvetica", 12, "bold")
        )
        tips_label.pack(anchor=tk.W, pady=(0, 10))

        tips_text = self.translator.translate("app", "tips_content")

        tips_info = ttk.Label(
            tips_frame,
            text=tips_text,
            wraplength=600,
            justify=tk.LEFT
        )
        tips_info.pack(fill=tk.X, pady=5)

        # About tab
        about_frame = ttk.Frame(notebook, padding=10)
        notebook.add(about_frame, text=self.translator.translate("app", "about_tab"))

        about_text = self.translator.translate("app", "about_content").format(
            version=APP_VERSION,
            update_date=APP_UPDATE_DATE,
            copyright=APP_COPYRIGHT
        )

        about_info = ttk.Label(
            about_frame,
            text=about_text,
            wraplength=600,
            justify=tk.CENTER
        )
        about_info.pack(fill=tk.BOTH, expand=True, pady=20)

        # Bottom buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=10)

        # Continue button
        continue_button = ttk.Button(
            button_frame,
            text=self.translator.translate("app", "get_started_button"),
            command=welcome_window.destroy,
            style="Accent.TButton"  # Using ttkbootstrap accent style if available
        )
        continue_button.pack(side=tk.RIGHT)

        # Add a custom style for the accent button if using plain ttk
        try:
            style = ttk.Style()
            style.configure("Accent.TButton", font=("Helvetica", 10, "bold"))
        except:
            pass

        # Wait for the dialog to be closed
        self.root.wait_window(welcome_window)

    def run_in_thread(self, func, callback=None, *args, **kwargs):
        """Run a function in a separate thread to prevent UI blocking

        Args:
            func: The function to run
            callback: Optional callback function to run with the result
            *args: Arguments to pass to the function
            **kwargs: Keyword arguments to pass to the function
        """

        def _thread_wrapper():
            try:
                result = func(*args, **kwargs)
                if callback:
                    # Schedule the callback in the main thread
                    self.root.after(0, lambda: callback(result))
            except Exception as e:
                # Schedule error handling in the main thread
                self.root.after(0, lambda: self._handle_error(e))

        # Create and start the thread
        thread = threading.Thread(target=_thread_wrapper)
        thread.daemon = True
        thread.start()

        return thread

    def _handle_error(self, error):
        """Handle an error from a background thread

        Args:
            error: The exception that was raised
        """
        messagebox.showerror(
            self.translator.translate("app", "error"),
            str(error)
        )

    def save_config(self):
        """Save current configuration"""
        self.config.save()

    def apply_theme(self, theme_setting):
        """
        动态应用新的主题设置

        Args:
            theme_setting (str): 主题设置 ('light', 'dark', 'system')

        Returns:
            bool: 是否成功应用主题
        """

        try:
            # 保存设置到配置
            self.config.set('theme', theme_setting)

            # 获取ttkbootstrap主题名
            theme_name = get_theme_name(theme_setting)

            # 更新样式
            style = ttk.Style()
            style.theme_use(theme_name)

            # 提示用户需要重启以获得最佳效果
            messagebox.showinfo(
                self.translator.translate("settings_tab", "theme_changed_title") or "Theme Changed",
                self.translator.translate("settings_tab", "theme_changed_message") or
                "Theme has been changed. Please restart the application for the best experience."
            )

            return True
        except Exception as e:
            messagebox.showerror(
                self.translator.translate("settings_tab", "error") or "Error",
                f"Failed to apply theme: {str(e)}"
            )
            return False

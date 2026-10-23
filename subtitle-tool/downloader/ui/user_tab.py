"""
User videos tab for TikHub Downloader with multilingual support
"""

import os
import time
import tkinter as tk
import ttkbootstrap as ttk
from tkinter import messagebox, scrolledtext
from datetime import datetime, timedelta

from downloader.utils.utils import format_timestamp, format_number, open_folder, sanitize_filename
from downloader.core.downloader import VideoDownloader
from downloader.utils.logger import logger_instance


class UserTab:
    """User videos tab UI and functionality"""

    def __init__(self, parent, app):
        """Initialize the user videos tab

        Args:
            parent: Parent widget
            app: Application instance
        """
        self.app = app
        self.frame = ttk.Frame(parent)
        self.translator = app.translator  # Access translator from app

        # Set the logger
        self.logger = logger_instance

        # User data
        self.user_profile = None
        self.user_videos = []
        self.user_platform = None  # To store the platform (Douyin or TikTok)

        # Download tracking
        self.download_start_time = None
        self.download_speed = []  # For calculating average download speed
        self.current_item_index = 0
        self.downloading = False

        # Create UI components
        self._create_widgets()

    def _configure_treeview_style(self):
        """Configure Treeview style for better readability"""
        try:
            style = ttk.Style()

            # Configure base Treeview style
            style.configure(
                "Treeview",
                background="#2c3e50",  # Dark background to match the app theme
                foreground="white",  # White text for high contrast
                fieldbackground="#2c3e50",  # Matching background for the field
                rowheight=25  # Slightly taller rows for better readability
            )

            # Alternating row colors for better visual separation
            style.map(
                "Treeview",
                background=[
                    ('selected', '#3498db'),  # Bright blue for selected rows
                    ('alternate', '#34495e')  # Slightly lighter shade for alternate rows
                ]
            )

            # Heading style
            style.configure(
                "Treeview.Heading",
                background="#2980b9",  # Darker blue for headings
                foreground="white",  # White text for headings
                font=('Helvetica', 10, 'bold')
            )

            # Scrollbar style to match the theme
            style.configure(
                "Vertical.TScrollbar",
                background="#2c3e50",
                arrowcolor="white"
            )

            # Apply the style to the Treeview
            self.videos_tree.configure(style="Treeview")
        except Exception as e:
            print(f"Error configuring Treeview style: {e}")

    def _create_widgets(self):
        """Create tab UI components"""
        # Main frame with padding
        main_frame = ttk.Frame(self.frame, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Top section - User URL input
        url_section = ttk.LabelFrame(main_frame, text=self.translator.translate("user_tab", "user_profile"), padding=10)
        url_section.pack(fill=tk.X, pady=(0, 10))

        # URL input row
        url_frame = ttk.Frame(url_section)
        url_frame.pack(fill=tk.X)

        user_label = ttk.Label(url_frame, text=self.translator.translate("user_tab", "user_url_label"))
        user_label.pack(side=tk.LEFT, padx=5)

        self.user_url_var = tk.StringVar()
        user_entry = ttk.Entry(url_frame, textvariable=self.user_url_var)
        user_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        paste_button = ttk.Button(url_frame, text=self.translator.translate("user_tab", "paste_button"),  command=self._paste_url)
        paste_button.pack(side=tk.LEFT, padx=5)

        clear_button = ttk.Button(url_frame, text=self.translator.translate("user_tab", "clear_button"),  command=lambda: self.user_url_var.set(""))
        clear_button.pack(side=tk.LEFT, padx=5)

        # Control buttons row
        control_frame = ttk.Frame(url_section)
        control_frame.pack(fill=tk.X, pady=(10, 0))

        get_info_button = ttk.Button(
            control_frame,
            text=self.translator.translate("user_tab", "get_user_info_button"),
            command=self._get_user_info,
            style="Accent.TButton"
        )
        get_info_button.pack(side=tk.LEFT, padx=5)

        # Max videos selection with frame
        max_videos_frame = ttk.Frame(control_frame)
        max_videos_frame.pack(side=tk.RIGHT)

        max_videos_label = ttk.Label(max_videos_frame, text=self.translator.translate("user_tab", "max_videos_label"))
        max_videos_label.pack(side=tk.LEFT, padx=(20, 5))

        self.max_videos_var = tk.IntVar(value=20)
        max_videos_spinner = ttk.Spinbox(
            max_videos_frame,
            from_=1,
            to=999,
            textvariable=self.max_videos_var
        )
        max_videos_spinner.pack(side=tk.LEFT, padx=5)

        # User info section
        info_section = ttk.LabelFrame(main_frame, text=self.translator.translate("user_tab", "user_information"), padding=10)
        info_section.pack(fill=tk.X, pady=(0, 10))

        self.user_info_text = scrolledtext.ScrolledText(info_section, height=4, wrap=tk.WORD)
        self.user_info_text.pack(fill=tk.X, pady=5)

        # Videos section - takes remaining space
        videos_section = ttk.LabelFrame(main_frame, text=self.translator.translate("user_tab", "user_videos"), padding=10)
        videos_section.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        videos_section.grid_columnconfigure(0, weight=1)

        # Create treeview with custom columns inside videos_section
        columns = ("Title", "Date", "Likes", "Type")
        self.videos_tree = ttk.Treeview(
            videos_section,
            columns=columns,
            selectmode="extended",
            show="headings"
        )

        # Define column headings
        self.videos_tree.heading("Title", text=self.translator.translate("user_tab", "column_title"), anchor=tk.W)
        self.videos_tree.heading("Date", text=self.translator.translate("user_tab", "column_date"), anchor=tk.CENTER)
        self.videos_tree.heading("Likes", text=self.translator.translate("user_tab", "column_likes"), anchor=tk.CENTER)
        self.videos_tree.heading("Type", text=self.translator.translate("user_tab", "column_type"), anchor=tk.CENTER)

        # Define column widths and properties
        self.videos_tree.column("Title",  anchor=tk.W, stretch=True)
        self.videos_tree.column("Date",  anchor=tk.CENTER, stretch=False)
        self.videos_tree.column("Likes", anchor=tk.CENTER, stretch=False)
        self.videos_tree.column("Type",  anchor=tk.CENTER, stretch=False)

        # Create tree view with scrollbar using grid
        y_scrollbar = ttk.Scrollbar(videos_section, orient="vertical", command=self.videos_tree.yview)
        x_scrollbar = ttk.Scrollbar(videos_section, orient="horizontal", command=self.videos_tree.xview)
        self.videos_tree.configure(yscrollcommand=y_scrollbar.set, xscrollcommand=x_scrollbar.set)

        # Use grid to position treeview and scrollbars precisely
        self.videos_tree.grid(row=0, column=0, sticky="nsew")
        y_scrollbar.grid(row=0, column=1, sticky="ns")
        x_scrollbar.grid(row=1, column=0, sticky="ew")

        # Configure grid weights
        videos_section.grid_rowconfigure(0, weight=1)
        videos_section.grid_columnconfigure(0, weight=1)

        # Button frame using grid to align with treeview
        button_frame = ttk.Frame(videos_section)
        button_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        # Left side - selection buttons
        selection_frame = ttk.Frame(button_frame)
        selection_frame.pack(side=tk.LEFT)

        select_all_button = ttk.Button(
            selection_frame,
            text=self.translator.translate("user_tab", "select_all_button"),
            command=self._select_all_videos
        )
        select_all_button.pack(side=tk.LEFT, padx=(0, 5))

        unselect_all_button = ttk.Button(
            selection_frame,
            text=self.translator.translate("user_tab", "unselect_all_button"),
            command=self._unselect_all_videos
        )
        unselect_all_button.pack(side=tk.LEFT)

        # Right side - download buttons
        download_frame = ttk.Frame(button_frame)
        download_frame.pack(side=tk.RIGHT)

        download_selected_button = ttk.Button(
            download_frame,
            text=self.translator.translate("user_tab", "download_selected_button"),
            style="Accent.TButton",
            command=self._download_selected_videos
        )
        download_selected_button.pack(side=tk.RIGHT, padx=(5, 0))

        download_all_button = ttk.Button(
            download_frame,
            text=self.translator.translate("user_tab", "download_all_button"),
            command=self._download_all_videos
        )
        download_all_button.pack(side=tk.RIGHT)

        # Progress section
        progress_section = ttk.LabelFrame(main_frame, text=self.translator.translate("user_tab", "download_progress"), padding=10)
        progress_section.pack(fill=tk.X)

        # Progress info frame
        info_frame = ttk.Frame(progress_section)
        info_frame.pack(fill=tk.X, pady=(0, 5))

        # Status label
        self.status_var = tk.StringVar(value=self.translator.translate("user_tab", "status_ready"))
        status_label = ttk.Label(info_frame, textvariable=self.status_var, anchor=tk.W)
        status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Progress counter
        self.progress_var = tk.StringVar(value="0/0")
        progress_label = ttk.Label(info_frame, textvariable=self.progress_var)
        progress_label.pack(side=tk.RIGHT)

        # Progress bar
        progress_frame = ttk.Frame(progress_section)
        progress_frame.pack(fill=tk.X)

        self.progress = ttk.Progressbar(progress_frame, style="Accent.Horizontal.TProgressbar")
        self.progress.pack(fill=tk.X)

        # Detailed progress indicators
        details_frame = ttk.Frame(progress_section)
        details_frame.pack(fill=tk.X, pady=(5, 0))

        # Current item
        current_frame = ttk.Frame(details_frame)
        current_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.current_item_var = tk.StringVar(value=self.translator.translate("user_tab", "current_item_none"))
        current_label = ttk.Label(current_frame, textvariable=self.current_item_var, anchor=tk.W)
        current_label.pack(side=tk.LEFT)

        # ETA
        eta_frame = ttk.Frame(details_frame)
        eta_frame.pack(side=tk.RIGHT)

        self.eta_var = tk.StringVar(value=self.translator.translate("user_tab", "eta_default"))
        eta_label = ttk.Label(eta_frame, textvariable=self.eta_var)
        eta_label.pack(side=tk.RIGHT)

        # Add item double-click event
        self.videos_tree.bind("<Double-1>", self._on_item_double_click)

        # Try to apply custom styles
        try:
            style = ttk.Style()
            style.configure("Accent.TButton", font=("Helvetica", 10, "bold"))
            style.configure("Accent.Horizontal.TProgressbar", background="#3f6ad8")
        except:
            pass

    def update_language(self):
        """Update the UI text when language changes"""
        # Update section titles
        for child in self.frame.winfo_children():
            if isinstance(child, ttk.Frame):
                for section in child.winfo_children():
                    if isinstance(section, ttk.LabelFrame):
                        if "user_profile" in str(section):
                            section.configure(text=self.translator.translate("user_tab", "user_profile"))
                        elif "user_information" in str(section):
                            section.configure(text=self.translator.translate("user_tab", "user_information"))
                        elif "user_videos" in str(section):
                            section.configure(text=self.translator.translate("user_tab", "user_videos"))
                        elif "download_progress" in str(section):
                            section.configure(text=self.translator.translate("user_tab", "download_progress"))

        # Update URL section
        for child in self.frame.winfo_children():
            if isinstance(child, ttk.Frame):
                for section in child.winfo_children():
                    if isinstance(section, ttk.LabelFrame) and "user_profile" in str(section):
                        for frame in section.winfo_children():
                            if isinstance(frame, ttk.Frame):
                                for widget in frame.winfo_children():
                                    if isinstance(widget, ttk.Label) and widget.winfo_width() < 100:
                                        widget.configure(text=self.translator.translate("user_tab", "user_url_label"))
                                    elif isinstance(widget, ttk.Button):
                                        if "paste" in str(widget).lower():
                                            widget.configure(text=self.translator.translate("user_tab", "paste_button"))
                                        elif "clear" in str(widget).lower():
                                            widget.configure(text=self.translator.translate("user_tab", "clear_button"))

                            for widget in frame.winfo_children():
                                if isinstance(widget, ttk.Button) and "get_user_info" in str(widget).lower():
                                    widget.configure(text=self.translator.translate("user_tab", "get_user_info_button"))
                                elif isinstance(widget, ttk.Label) and "max_videos" in str(widget).lower():
                                    widget.configure(text=self.translator.translate("user_tab", "max_videos_label"))

        # Update treeview headings
        self.videos_tree.heading("Title", text=self.translator.translate("user_tab", "column_title"))
        self.videos_tree.heading("Date", text=self.translator.translate("user_tab", "column_date"))
        self.videos_tree.heading("Likes", text=self.translator.translate("user_tab", "column_likes"))
        self.videos_tree.heading("Type", text=self.translator.translate("user_tab", "column_type"))

        # Update selection/download buttons
        for child in self.frame.winfo_children():
            if isinstance(child, ttk.Frame):
                for section in child.winfo_children():
                    if isinstance(section, ttk.LabelFrame) and "user_videos" in str(section):
                        for frame in section.winfo_children():
                            if isinstance(frame, ttk.Frame):
                                for button_frame in frame.winfo_children():
                                    if isinstance(button_frame, ttk.Frame):
                                        for button in button_frame.winfo_children():
                                            if isinstance(button, ttk.Button):
                                                if "select_all" in str(button).lower():
                                                    button.configure(text=self.translator.translate("user_tab", "select_all_button"))
                                                elif "unselect_all" in str(button).lower():
                                                    button.configure(text=self.translator.translate("user_tab", "unselect_all_button"))
                                                elif "download_selected" in str(button).lower():
                                                    button.configure(text=self.translator.translate("user_tab", "download_selected_button"))
                                                elif "download_all" in str(button).lower():
                                                    button.configure(text=self.translator.translate("user_tab", "download_all_button"))

        # Update status text if it's the default value
        if self.status_var.get() in ["Ready", "就绪"]:
            self.status_var.set(self.translator.translate("user_tab", "status_ready"))

        # Update current item and ETA if they're the default values
        if "Current item: None" in self.current_item_var.get() or "当前项目：无" in self.current_item_var.get():
            self.current_item_var.set(self.translator.translate("user_tab", "current_item_none"))

        if "ETA: --:--" in self.eta_var.get() or "预计完成时间: --:--" in self.eta_var.get():
            self.eta_var.set(self.translator.translate("user_tab", "eta_default"))

    def _paste_url(self):
        """Paste clipboard content to URL input"""
        try:
            clipboard_content = self.frame.clipboard_get().strip()
            if clipboard_content:
                self.user_url_var.set(clipboard_content)
            else:
                messagebox.showwarning(
                    self.translator.translate("user_tab", "warning_title"),
                    self.translator.translate("user_tab", "clipboard_empty")
                )
        except Exception:
            messagebox.showwarning(
                self.translator.translate("user_tab", "warning_title"),
                self.translator.translate("user_tab", "clipboard_error")
            )

    def _get_user_info(self):
        """Handle get user info button click"""
        user_url = self.user_url_var.get().strip()
        if not user_url:
            messagebox.showwarning(
                self.translator.translate("user_tab", "warning_title"),
                self.translator.translate("user_tab", "enter_user_url")
            )
            return

        if not self.app.client.is_configured:
            messagebox.showwarning(
                self.translator.translate("user_tab", "warning_title"),
                self.translator.translate("user_tab", "setup_api_key_first")
            )
            return

        # Update status
        status_msg = self.translator.translate("user_tab", "getting_user_info")
        self.status_var.set(status_msg)
        self.app.main_window.update_status(f"{status_msg}: {user_url}")

        # Clear previous data
        self._clear_user_data()

        # Get user info in background thread
        max_videos = self.max_videos_var.get()
        self.app.run_in_thread(
            self.app.client.get_user_info_and_videos,
            self._on_user_info_received,
            user_url,
            max_videos
        )

    def _clear_user_data(self):
        """Clear user data and UI"""
        # Clear tree view
        for item in self.videos_tree.get_children():
            self.videos_tree.delete(item)

        # Clear user info
        self.user_info_text.delete(1.0, tk.END)

        # Clear stored data
        self.user_profile = None
        self.user_videos = []

    def _on_user_info_received(self, result):
        """Handle user info received

        Args:
            result: (user_profile, user_videos) tuple
        """
        user_profile, user_videos, platform = result

        # Store user data
        self.user_profile = user_profile
        self.user_videos = user_videos
        self.user_platform = platform

        if not self.user_profile:
            self.status_var.set(self.translator.translate("user_tab", "failed_get_user_info"))
            self.app.main_window.update_status(self.translator.translate("user_tab", "failed_get_user_info"))
            messagebox.showerror(
                self.translator.translate("user_tab", "error_title"),
                self.translator.translate("user_tab", "user_info_error_message")
            )
            return

        # Display user info
        self._display_user_info(user_profile)

        # Media Type Code Mapping
        media_type_codes = {
            # common
            0: self.translator.translate("user_tab", "media_type_video"),
            # Douyin
            2: self.translator.translate("user_tab", "media_type_image"),
            4: self.translator.translate("user_tab", "media_type_video"),
            68: self.translator.translate("user_tab", "media_type_image"),
            # TikTok
            51: self.translator.translate("user_tab", "media_type_video"),
            55: self.translator.translate("user_tab", "media_type_video"),
            58: self.translator.translate("user_tab", "media_type_video"),
            61: self.translator.translate("user_tab", "media_type_video"),
            150: self.translator.translate("user_tab", "media_type_image")
        }

        # Display videos in the tree
        for video in user_videos:
            try:
                aweme_id = video.get('aweme_id', 'Unknown')
                desc = video.get('desc', self.translator.translate("user_tab", "no_description"))
                if not desc:
                    desc = self.translator.translate("user_tab", "no_description")
                create_time = video.get('create_time', 0)
                digg_count = video.get('statistics', {}).get('digg_count', 0)
                media_type = media_type_codes.get(video.get('aweme_type', 0), self.translator.translate("user_tab", "media_type_video"))

                # Display collection/album count if it's an image post with multiple images
                if media_type == self.translator.translate("user_tab", "media_type_image") and 'images' in video:
                    image_count = len(video.get('images', []))
                    if image_count > 1:
                        media_type = f"{self.translator.translate('user_tab', 'media_type_album')}({image_count})"

                # Convert timestamp to readable date
                date_str = format_timestamp(create_time).split()[0]  # Just the date part

                # Add to tree view
                item_id = self.videos_tree.insert(
                    "", tk.END,
                    values=(desc, date_str, format_number(digg_count), media_type)
                )

                # Store the aweme_id as a tag for retrieval later
                self.videos_tree.item(item_id, tags=(aweme_id,))

                # Add alternating row colors
                if len(self.videos_tree.get_children()) % 2 == 0:
                    self.videos_tree.item(item_id, tags=(aweme_id, "even"))
                else:
                    self.videos_tree.item(item_id, tags=(aweme_id, "odd"))

            except Exception as e:
                print(f"Error adding video to tree: {e}")

        # Update status
        video_count = len(user_videos)
        self.status_var.set(self.translator.translate("user_tab", "found_videos").format(count=video_count))
        self.app.main_window.update_status(self.translator.translate("user_tab", "found_user_videos").format(count=video_count))

        # Try to configure alternating row colors
        try:
            style = ttk.Style()
            style.map('Treeview')
            style.configure("Treeview")
            style.configure("Treeview", rowheight=25)  # Taller rows
            style.map("Treeview")
        except:
            pass

    def _display_user_info(self, user_profile):
        """Display user information

        Args:
            user_profile: User profile dictionary
        """
        try:
            # Clear previous info
            self.user_info_text.delete(1.0, tk.END)

            # Get user info
            user_info = user_profile.get('user', {})

            # Build formatted info with tab-aligned values
            info = f"{self.translator.translate('user_tab', 'username_label')}   {user_info.get('nickname', self.translator.translate('user_tab', 'unknown'))}\n"
            info += f"{self.translator.translate('user_tab', 'secuserid_label')}  {user_info.get('sec_uid', self.translator.translate('user_tab', 'unknown'))}\n"
            info += f"{self.translator.translate('user_tab', 'uid_label')}        {user_info.get('uid', self.translator.translate('user_tab', 'unknown'))}\n"

            # Add more details in a cleaner layout
            details = []
            if 'signature' in user_info and user_info['signature']:
                details.append(f"{self.translator.translate('user_tab', 'signature_label')}  {user_info.get('signature', self.translator.translate('user_tab', 'no_signature'))}")
            if 'ip_location' in user_info:
                details.append(f"{self.translator.translate('user_tab', 'location_label')}   {user_info.get('ip_location', self.translator.translate('user_tab', 'unknown'))}")
            if 'user_age' in user_info:
                details.append(f"{self.translator.translate('user_tab', 'age_label')}        {user_info.get('user_age', self.translator.translate('user_tab', 'unknown'))}")
            if 'share_info' in user_info and 'share_url' in user_info['share_info']:
                share_url = user_info.get('share_info', {}).get('share_url', '').split('?')[0]
                if share_url:
                    details.append(f"{self.translator.translate('user_tab', 'profile_label')}    {share_url}")

            # Add selected details if present
            if details:
                info += "\n" + "\n".join(details) + "\n"

            # Add statistics in a clear format
            stats = []
            following_count = user_info.get('following_count', 0)
            follower_count = user_info.get('follower_count', 0)
            total_favorited = user_info.get('total_favorited', 0)

            stats.append(f"{self.translator.translate('user_tab', 'following_label')}  {format_number(following_count)}")
            stats.append(f"{self.translator.translate('user_tab', 'followers_label')}  {format_number(follower_count)}")
            stats.append(f"{self.translator.translate('user_tab', 'total_likes_label')} {format_number(total_favorited)}")

            if stats:
                info += "\n" + "\n".join(stats)

            # Display info
            self.user_info_text.insert(tk.END, info)

            # Configure tags for styling
            try:
                self.user_info_text.tag_configure("bold", font=("Helvetica", 10, "bold"))
                for line in info.split('\n'):
                    if ':' in line:
                        key, value = line.split(':', 1)
                        start_pos = info.find(line)
                        colon_pos = start_pos + len(key) + 1
                        self.user_info_text.tag_add("bold", f"1.{start_pos}", f"1.{colon_pos}")
            except:
                pass

            # Make text read-only
            self.user_info_text.config(state=tk.DISABLED)

        except Exception as e:
            self.user_info_text.insert(tk.END, f"{self.translator.translate('user_tab', 'parse_user_info_error')}: {str(e)}")

    def _select_all_videos(self):
        """Select all videos in the tree"""
        for item in self.videos_tree.get_children():
            self.videos_tree.selection_add(item)

    def _unselect_all_videos(self):
        """Unselect all videos"""
        self.videos_tree.selection_remove(self.videos_tree.selection())

    def _download_all_videos(self):
        """Download all user videos"""
        if not self.user_videos:
            messagebox.showwarning(
                self.translator.translate("user_tab", "warning_title"),
                self.translator.translate("user_tab", "no_videos_to_download")
            )
            return

        if not self.app.client.is_configured:
            messagebox.showwarning(
                self.translator.translate("user_tab", "warning_title"),
                self.translator.translate("user_tab", "setup_api_key_first")
            )
            return

        # Check if download is already in progress
        if self.downloading:
            messagebox.showinfo(
                self.translator.translate("user_tab", "info_title"),
                self.translator.translate("user_tab", "download_in_progress")
            )
            return

        # Select all videos
        self._select_all_videos()

        # Download selected
        self._download_selected_videos()

    def _download_selected_videos(self):
        """Download selected videos"""
        selected_items = self.videos_tree.selection()
        if not selected_items:
            messagebox.showwarning(
                self.translator.translate("user_tab", "warning_title"),
                self.translator.translate("user_tab", "select_videos_to_download")
            )
            return

        if not self.app.client.is_configured:
            messagebox.showwarning(
                self.translator.translate("user_tab", "warning_title"),
                self.translator.translate("user_tab", "setup_api_key_first")
            )
            return

        # Check if download is already in progress
        if self.downloading:
            messagebox.showinfo(
                self.translator.translate("user_tab", "info_title"),
                self.translator.translate("user_tab", "download_in_progress")
            )
            return

        # Set download flag
        self.downloading = True
        self.download_start_time = time.time()
        self.download_speed = []

        # Get settings
        settings = {}
        if hasattr(self.app, 'settings_tab'):
            settings = self.app.settings_tab.get_settings()

        # Create downloader with current settings
        downloader = VideoDownloader(
            self.app.download_path,
            use_description=settings.get('rename_with_desc', False),
            skip_existing=settings.get('skip_existing', True),
            postprocess_enabled=settings.get('postprocess_enabled', False),
            trim_start_seconds=settings.get('postprocess_trim_start_seconds', 1.5),
            mirror_video=settings.get('postprocess_mirror_video', True),
            delete_original_after_processing=settings.get('postprocess_delete_original', False),
            ffmpeg_path=settings.get('ffmpeg_path'),
            subtitle_cover_enabled=settings.get('subtitle_cover_enabled', False),
            subtitle_cover_workspace=settings.get('subtitle_cover_workspace'),
            subtitle_cover_detect_fps=settings.get('subtitle_cover_detect_fps', 1.5),
            subtitle_cover_mode=settings.get('subtitle_cover_mode', 'blur'),
            subtitle_ai_workspace=settings.get('subtitle_ai_workspace'),
            subtitle_ai_auto_detect=settings.get('subtitle_ai_auto_detect', True),
            subtitle_ai_require_cuda=settings.get('subtitle_ai_require_cuda', True),
            subtitle_ai_quality=settings.get('subtitle_ai_quality', 18)
        )

        # Prepare user folder
        user_folder = self._prepare_user_folder()

        # Start download in background thread
        self.status_var.set(self.translator.translate("user_tab", "preparing_download"))
        self.app.main_window.update_status(self.translator.translate("user_tab", "preparing_download"))
        self.app.run_in_thread(
            self._download_selected_thread,
            self._on_batch_download_complete,
            selected_items=selected_items,
            downloader=downloader,
            user_folder=user_folder
        )

    def _prepare_user_folder(self):
        """Prepare user folder for downloads

        Returns:
            str: Path to user folder
        """
        user_folder = self.app.download_path

        if self.user_profile and 'user' in self.user_profile:
            # Use username for folder
            nickname = self.user_profile['user'].get('nickname', '')
            if nickname:
                safe_nickname = sanitize_filename(nickname)
                user_folder = os.path.join(self.app.download_path, safe_nickname)
                os.makedirs(user_folder, exist_ok=True)

        return user_folder

    def _download_selected_thread(self, selected_items, downloader, user_folder):
        """Background thread function for downloading selected videos

        Args:
            selected_items: Selected tree items
            downloader: VideoDownloader instance
            user_folder: Folder to save videos to

        Returns:
            tuple: (completed, failed, total)
        """
        total = len(selected_items)
        completed = 0
        failed = 0
        has_warnings = False

        # Reset current item index
        self.current_item_index = 0

        # Set up progress tracking
        self._update_progress(0, total)

        # Media Type Code Mapping
        media_type_codes = {
            # common
            0: 'video',
            # Douyin
            2: 'image',
            4: 'video',
            68: 'image',
            # TikTok
            51: 'video',
            55: 'video',
            58: 'video',
            61: 'video',
            150: 'image'
        }

        # Update ETA initially
        self._update_eta(0, total)

        for i, item in enumerate(selected_items):
            # Store current item index for UI updates
            self.current_item_index = i

            # Get video ID from item tag
            aweme_id = self.videos_tree.item(item, "tags")[0]

            # Get item description from values
            item_desc = self.videos_tree.item(item, "values")[0]
            if not item_desc or item_desc == self.translator.translate("user_tab", "no_description"):
                item_desc = f"{self.translator.translate('user_tab', 'item')} {i + 1}"

            # Truncate description if too long
            if len(item_desc) > 40:
                item_desc = item_desc[:37] + "..."

            # Update UI
            self._update_progress(i, total)
            self._update_current_item(f"{item_desc}")
            self.app.main_window.update_status(
                f"{self.translator.translate('user_tab', 'processing_video')} {i + 1}/{total}: {aweme_id}")

            # Track this item's start time for speed calculation
            item_start_time = time.time()

            # Find the video in the user videos list
            video_info = None
            for video in self.user_videos:
                if video.get('aweme_id') == aweme_id:
                    video_info = video
                    break

            if not video_info:
                failed += 1
                continue

            # Check media type
            media_type = media_type_codes.get(video_info.get('aweme_type', 0), 'video')

            try:
                # Prepare data structure based on media type
                if media_type == 'video':
                    # Get play URLs
                    play_url = self._get_play_url_from_video(video_info)
                    if not play_url:
                        failed += 1
                        continue

                    # Structure data for downloader
                    structured_data = {
                        'id': video_info.get('aweme_id', ''),
                        'desc': video_info.get('desc', ''),
                        'author_name': video_info.get('author', {}).get('nickname', ''),
                        'author_id': video_info.get('author', {}).get('uid', ''),
                        'platform': self.user_platform,
                        'media_type': 'video',
                        'create_time': video_info.get('create_time', ''),
                        'video_urls': play_url if isinstance(play_url, list) else [play_url]
                    }

                else:  # Image or album
                    # Get image URLs
                    image_urls = self._get_image_url_from_video(video_info)
                    if not image_urls:
                        failed += 1
                        continue

                    # Structure data for downloader
                    structured_data = {
                        'id': video_info.get('aweme_id', ''),
                        'desc': video_info.get('desc', ''),
                        'author_name': video_info.get('author', {}).get('nickname', ''),
                        'author_id': video_info.get('author', {}).get('uid', ''),
                        'platform': self.user_platform,
                        'media_type': 'image',
                        'create_time': video_info.get('create_time', ''),
                        'image_urls': image_urls
                    }

                # Download using main_downloader
                result = downloader.main_downloader(
                    structured_data,
                    output_dir=user_folder,
                    max_retries=3
                )

                # Check result
                if result['success'] and result['files']:
                    completed += 1
                    if result['errors']:
                        has_warnings = True
                        self.logger.warning("Download completed with warnings: %s", " | ".join(result['errors']))
                    # Calculate download speed
                    item_time = time.time() - item_start_time
                    if item_time > 0:
                        # Keep only the last 5 speeds for a moving average
                        self.download_speed.append(1 / item_time)
                        if len(self.download_speed) > 5:
                            self.download_speed.pop(0)
                else:
                    failed += 1

                # Update ETA
                self._update_eta(i + 1, total)

            except Exception as e:
                print(f"Error downloading video {aweme_id}: {e}")
                failed += 1

        # Final progress update
        self._update_progress(total, total)
        self._update_current_item(self.translator.translate("user_tab", "download_complete"))
        self.eta_var.set(
            f"{self.translator.translate('user_tab', 'eta_prefix')} {self.translator.translate('user_tab', 'complete')}")

        # Reset download flag
        self.downloading = False

        warning_message = "Some items downloaded with post-processing warnings. Check the log for details."
        return completed, failed, total, warning_message if has_warnings else None

    def _get_play_url_from_video(self, video_info) -> list:
        """Extract play URL from video info

        Args:
            video_info: Video info dictionary

        Returns:
            list: List of play URLs or None if not found
        """
        # Try direct extraction from video data
        try:
            if 'video' in video_info:
                video = video_info['video']

                # Try different formats
                for key in ['play_addr_265', 'play_addr_h264', 'play_addr', 'download_addr']:
                    if key in video and 'url_list' in video[key] and video[key]['url_list']:
                        return [video[key]['url_list'][0]]
        except Exception:
            pass

        # Return None if not found
        return None

    def _get_image_url_from_video(self, video_info):
        """Extract image URL from video info

        Args:
            video_info: Video info dictionary

        Returns:
            list: List of image URLs or None if not found
        """
        # Try direct extraction from video data
        try:
            if self.user_platform == 'douyin':
                if video_info.get("images") and isinstance(video_info.get("images"), list):
                    all_urls = [
                        (image.get("url_list") or [""])[0]
                        for image in video_info.get("images", [])
                    ]
                else:
                    all_urls = []
            elif self.user_platform == 'tiktok':
                if 'image_post_info' in video_info:
                    all_urls = [
                        (image.get("display_image", {}).get("url_list") or [""])[0]
                        for image in video_info.get("image_post_info", {}).get("images", [])
                    ]
                else:
                    all_urls = []

            else:
                all_urls = []

            if all_urls:
                self.logger.debug(f"Found {len(all_urls)} image URLs")
                return all_urls
        except Exception as e:
            self.logger.error(f"Error extracting image URLs: {e}")

        # Return None if not found
        self.logger.warning("No valid image URLs found")
        return None

    def _update_progress(self, current, total):
        """Update progress display

        Args:
            current: Current progress
            total: Total items
        """
        if total > 0:
            value = int((current / total) * 100)
        else:
            value = 0

        self.progress_var.set(f"{current}/{total}")
        self.progress["value"] = value

        # Update status line
        percentage = f"{value}%" if total > 0 else "0%"
        if current == total and current > 0:
            self.status_var.set(f"{self.translator.translate('user_tab', 'download_complete')} - {percentage}")
        elif current > 0:
            self.status_var.set(f"{self.translator.translate('user_tab', 'downloading')} - {percentage}")

        self.app.root.update_idletasks()

    def _update_current_item(self, item_text):
        """Update current item display

        Args:
            item_text: Description of current item
        """
        self.current_item_var.set(f"{self.translator.translate('user_tab', 'current_label')}: {item_text}")
        self.app.root.update_idletasks()

    def _update_eta(self, completed, total):
        """Update estimated time remaining

        Args:
            completed: Number of completed items
            total: Total number of items
        """
        if completed == 0 or total == 0:
            self.eta_var.set(f"{self.translator.translate('user_tab', 'eta_prefix')} {self.translator.translate('user_tab', 'calculating')}")
            return

        if completed == total:
            self.eta_var.set(f"{self.translator.translate('user_tab', 'eta_prefix')} {self.translator.translate('user_tab', 'complete')}")
            return

        # Calculate time elapsed and remaining
        elapsed = time.time() - self.download_start_time

        # Calculate average speed (items per second)
        if self.download_speed:
            avg_speed = sum(self.download_speed) / len(self.download_speed)
        else:
            avg_speed = completed / elapsed if elapsed > 0 else 0

        # Calculate remaining time
        if avg_speed > 0:
            remaining_items = total - completed
            eta_seconds = remaining_items / avg_speed

            # Format ETA
            if eta_seconds < 60:
                eta_text = f"{int(eta_seconds)}s"
            elif eta_seconds < 3600:
                minutes = int(eta_seconds / 60)
                seconds = int(eta_seconds % 60)
                eta_text = f"{minutes}m {seconds}s"
            else:
                hours = int(eta_seconds / 3600)
                minutes = int((eta_seconds % 3600) / 60)
                eta_text = f"{hours}h {minutes}m"

            # Calculate completion time
            now = datetime.now()
            completion_time = now + timedelta(seconds=eta_seconds)
            completion_str = completion_time.strftime("%H:%M:%S")

            # Update ETA display
            self.eta_var.set(f"{self.translator.translate('user_tab', 'eta_prefix')} {eta_text} ({self.translator.translate('user_tab', 'at')} {completion_str})")
        else:
            self.eta_var.set(f"{self.translator.translate('user_tab', 'eta_prefix')} {self.translator.translate('user_tab', 'calculating')}")

        self.app.root.update_idletasks()

    def _on_batch_download_complete(self, result):
        """Handle batch download completion

        Args:
            result: (completed, failed, total) tuple
        """
        completed, failed, total, warning_message = result

        # Reset download flag
        self.downloading = False

        # Update progress display
        self._update_progress(total, total)
        self.current_item_var.set(self.translator.translate("user_tab", "download_complete"))
        self.eta_var.set(f"{self.translator.translate('user_tab', 'eta_prefix')} {self.translator.translate('user_tab', 'complete')}")

        # Calculate total download time
        if self.download_start_time:
            total_time = time.time() - self.download_start_time
            if total_time > 60:
                time_str = f"{int(total_time / 60)} {self.translator.translate('user_tab', 'minutes')} {int(total_time % 60)} {self.translator.translate('user_tab', 'seconds')}"
            else:
                time_str = f"{int(total_time)} {self.translator.translate('user_tab', 'seconds')}"
            time_info = f"\n{self.translator.translate('user_tab', 'total_time')}: {time_str}"
        else:
            time_info = ""

        # Update status
        self.status_var.set(f"{self.translator.translate('user_tab', 'download_complete')}: {completed}/{total} {self.translator.translate('user_tab', 'items')}")
        self.app.main_window.update_status(f"{self.translator.translate('user_tab', 'completed_msg')} {completed}/{total}, {self.translator.translate('user_tab', 'failed_label')}: {failed}")

        # Get settings
        settings = {}
        if hasattr(self.app, 'settings_tab'):
            settings = self.app.settings_tab.get_settings()

        # Format completion message
        if completed > 0:
            message = f"{self.translator.translate('user_tab', 'download_summary')}\n\n"
            message += f"{self.translator.translate('user_tab', 'completed_label')}: {completed} {self.translator.translate('user_tab', 'items')}\n"
            message += f"{self.translator.translate('user_tab', 'failed_label')}: {failed} {self.translator.translate('user_tab', 'items')}\n"
            message += f"{self.translator.translate('user_tab', 'total_label')}: {total} {self.translator.translate('user_tab', 'items')}{time_info}"

            # Ask if user wants to open the folder
            open_folder_question = f"\n\n{self.translator.translate('user_tab', 'open_folder_question')}"

            # Use custom dialog instead of simple messagebox
            self._show_download_complete_dialog(message, completed)
            if warning_message:
                messagebox.showwarning(
                    self.translator.translate("user_tab", "warning_title"),
                    warning_message
                )
        else:
            messagebox.showinfo(
                self.translator.translate("user_tab", "download_complete"),
                f"{self.translator.translate('user_tab', 'completed_msg')} {completed}/{total}\n{self.translator.translate('user_tab', 'failed_label')}: {failed}{time_info}"
            )

    def _show_download_complete_dialog(self, message, completed):
        """Show a custom download completion dialog with option to open folder

        Args:
            message: The summary message
            completed: Number of completed downloads
        """
        # Create dialog
        dialog = tk.Toplevel(self.app.root)
        dialog.title(self.translator.translate("user_tab", "download_complete"))
        dialog.geometry("550x400")
        dialog.minsize(550, 400)

        # Make it modal
        dialog.transient(self.app.root)
        dialog.grab_set()

        # Center the dialog
        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = (self.app.root.winfo_width() - width) // 2 + self.app.root.winfo_x()
        y = (self.app.root.winfo_height() - height) // 2 + self.app.root.winfo_y()
        dialog.geometry(f"{width}x{height}+{x}+{y}")

        # Main frame with padding
        main_frame = ttk.Frame(dialog, padding=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Success icon and header
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill=tk.X, pady=(0, 10))

        # Create checkmark icon label
        check_label = ttk.Label(header_frame, text="✓", font=("Helvetica", 24, "bold"), foreground="green")
        check_label.pack(side=tk.LEFT, padx=(0, 10))

        # Header text
        header_label = ttk.Label(
            header_frame,
            text=self.translator.translate("user_tab", "download_complete"),
            font=("Helvetica", 14, "bold")
        )
        header_label.pack(side=tk.LEFT)

        # Message area
        message_area = ttk.Label(
            main_frame,
            text=message,
            justify=tk.LEFT,
            wraplength=360
        )
        message_area.pack(fill=tk.BOTH, expand=True, pady=10)

        # Button frame
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=(10, 0))

        # Only show open folder button if there are completed downloads
        if completed > 0:
            # Get user folder
            user_folder = self._prepare_user_folder()

            # Open folder button
            open_button = ttk.Button(
                button_frame,
                text=self.translator.translate("user_tab", "open_folder_button"),
                style="Accent.TButton",
                command=lambda: [open_folder(user_folder), dialog.destroy()]
            )
            open_button.pack(side=tk.LEFT, padx=(0, 10))

        # Close button
        close_button = ttk.Button(
            button_frame,
            text=self.translator.translate("user_tab", "close_button"),
            command=dialog.destroy
        )
        close_button.pack(side=tk.RIGHT)

        # Wait for the dialog to close
        self.app.root.wait_window(dialog)

    def _on_item_double_click(self, event):
        """Handle double-click on a video item

        Args:
            event: The event data
        """
        # Get selected item
        selection = self.videos_tree.selection()
        if not selection:
            return

        # Get item data
        item = selection[0]
        aweme_id = self.videos_tree.item(item, "tags")[0]
        values = self.videos_tree.item(item, "values")

        if not values:
            return

        # Get title and media type
        title = values[0]
        media_type = values[3] if len(values) > 3 else self.translator.translate("user_tab", "unknown")

        # Show details dialog
        self._show_video_details_dialog(aweme_id, title, media_type)

    def _show_video_details_dialog(self, aweme_id, title, media_type):
        """Show a dialog with detailed video information

        Args:
            aweme_id: The video ID
            title: The video title
            media_type: The media type (Video/Image/Album)
        """
        # Find the video info
        video_info = None
        for video in self.user_videos:
            if video.get('aweme_id') == aweme_id:
                video_info = video
                break

        if not video_info:
            return

        # Create dialog
        dialog = tk.Toplevel(self.app.root)
        dialog.title(f"{media_type} {self.translator.translate('user_tab', 'details')}")
        dialog.geometry("1000x800")
        dialog.minsize(1000, 800)

        # Make it modal
        dialog.transient(self.app.root)
        dialog.grab_set()

        # Center the dialog
        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = (self.app.root.winfo_width() - width) // 2 + self.app.root.winfo_x()
        y = (self.app.root.winfo_height() - height) // 2 + self.app.root.winfo_y()
        dialog.geometry(f"{width}x{height}+{x}+{y}")

        # Main frame with padding
        main_frame = ttk.Frame(dialog, padding=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Title and ID section
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill=tk.X, pady=(0, 10))

        # Title label
        title_label = ttk.Label(
            header_frame,
            text=title,
            font=("Helvetica", 12, "bold"),
            wraplength=460
        )
        title_label.pack(anchor=tk.W)

        # ID label
        id_label = ttk.Label(
            header_frame,
            text=f"ID: {aweme_id}"
        )
        id_label.pack(anchor=tk.W, pady=(5, 0))

        # Create notebook for tabs
        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        # Details tab
        details_frame = ttk.Frame(notebook, padding=10)
        notebook.add(details_frame, text=self.translator.translate("user_tab", "details_tab"))

        # Statistics tab
        stats_frame = ttk.Frame(notebook, padding=10)
        notebook.add(stats_frame, text=self.translator.translate("user_tab", "statistics_tab"))

        # Author tab
        author_frame = ttk.Frame(notebook, padding=10)
        notebook.add(author_frame, text=self.translator.translate("user_tab", "author_tab"))

        # Fill details tab
        self._fill_details_tab(details_frame, video_info)

        # Fill statistics tab
        self._fill_statistics_tab(stats_frame, video_info)

        # Fill author tab
        self._fill_author_tab(author_frame, video_info)

        # Button frame at bottom
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=(10, 0))

        # Download button
        download_button = ttk.Button(
            button_frame,
            text=self.translator.translate("user_tab", "download_now_button"),
            style="Accent.TButton",
            command=lambda: [self._download_single_video(video_info), dialog.destroy()]
        )
        download_button.pack(side=tk.LEFT)

        # Close button
        close_button = ttk.Button(
            button_frame,
            text=self.translator.translate("user_tab", "close_button"),
            command=dialog.destroy
        )
        close_button.pack(side=tk.RIGHT)

        # Wait for the dialog to close
        self.app.root.wait_window(dialog)

    def _fill_details_tab(self, parent, video_info):
        """Fill the details tab with video information

        Args:
            parent: The parent frame
            video_info: The video information dictionary
        """
        # Create scrolled text area
        details_text = scrolledtext.ScrolledText(parent, wrap=tk.WORD, height=10)
        details_text.pack(fill=tk.BOTH, expand=True)

        # Compile details
        details = ""

        # Basic info
        if 'desc' in video_info and video_info['desc']:
            details += f"{self.translator.translate('user_tab', 'description_label')}: {video_info['desc']}\n\n"

        if 'create_time' in video_info:
            create_time = format_timestamp(video_info['create_time'])
            details += f"{self.translator.translate('user_tab', 'created_label')}: {create_time}\n"

        if 'media_type' in video_info:
            media_type_codes = {
                0: self.translator.translate("user_tab", "media_type_video"),
                2: self.translator.translate("user_tab", "media_type_image"),
                4: self.translator.translate("user_tab", "media_type_video"),
                68: self.translator.translate("user_tab", "media_type_image"),
                51: self.translator.translate("user_tab", "media_type_video"),
                55: self.translator.translate("user_tab", "media_type_video"),
                58: self.translator.translate("user_tab", "media_type_video"),
                61: self.translator.translate("user_tab", "media_type_video"),
                150: self.translator.translate("user_tab", "media_type_image")
            }
            media_type = media_type_codes.get(video_info.get('aweme_type', 0), self.translator.translate("user_tab", "unknown"))
            details += f"{self.translator.translate('user_tab', 'media_type_label')}: {media_type}\n"

        # Image specific info
        if video_info.get("images", []):
            image_count = len(video_info.get('images', []))
            details += f"{self.translator.translate('user_tab', 'image_count_label')}: {image_count}\n"

        # Video specific info
        if 'video' in video_info:
            video = video_info['video']
            if 'duration' in video:
                duration_ms = video.get('duration', 0)
                duration_sec = duration_ms / 1000
                details += f"{self.translator.translate('user_tab', 'duration_label')}: {duration_sec:.2f} {self.translator.translate('user_tab', 'seconds')}\n"

            if 'ratio' in video:
                details += f"{self.translator.translate('user_tab', 'aspect_ratio_label')}: {video.get('ratio', self.translator.translate('user_tab', 'unknown'))}\n"

            # Resolution
            width = video.get('width', 0)
            height = video.get('height', 0)
            if width and height:
                details += f"{self.translator.translate('user_tab', 'resolution_label')}: {width}x{height}\n"

        # Add other available information
        if 'region' in video_info:
            details += f"{self.translator.translate('user_tab', 'region_label')}: {video_info.get('region', self.translator.translate('user_tab', 'unknown'))}\n"

        if 'geofencing' in video_info:
            geo_info = video_info.get('geofencing', {})
            if geo_info and 'country' in geo_info:
                details += f"{self.translator.translate('user_tab', 'geofencing_country_label')}: {geo_info.get('country')}\n"
            if geo_info and 'province' in geo_info:
                details += f"{self.translator.translate('user_tab', 'geofencing_province_label')}: {geo_info.get('province')}\n"

        # Add any video hashtags
        if 'text_extra' in video_info:
            hashtags = [item.get('hashtag_name') for item in video_info.get('text_extra', [])
                        if 'hashtag_name' in item and item.get('hashtag_name')]
            if hashtags:
                details += f"\n{self.translator.translate('user_tab', 'hashtags_label')}: {', '.join(hashtags)}\n"

        # Insert the details
        details_text.insert(tk.END, details)
        details_text.config(state=tk.DISABLED)  # Make read-only

    def _fill_statistics_tab(self, parent, video_info):
        """Fill the statistics tab with video metrics

        Args:
            parent: The parent frame
            video_info: The video information dictionary
        """
        # Create frame for stats
        stats_frame = ttk.Frame(parent)
        stats_frame.pack(fill=tk.BOTH, expand=True)

        # Get statistics
        stats = video_info.get('statistics', {})

        # Create a grid of stats
        row = 0

        # Define stat items to display
        stat_items = [
            (self.translator.translate("user_tab", "likes_label"), stats.get('digg_count', 0)),
            (self.translator.translate("user_tab", "comments_label"), stats.get('comment_count', 0)),
            (self.translator.translate("user_tab", "shares_label"), stats.get('share_count', 0)),
            (self.translator.translate("user_tab", "plays_label"), stats.get('play_count', 0)),
            (self.translator.translate("user_tab", "downloads_label"), stats.get('download_count', 0)),
            (self.translator.translate("user_tab", "forwards_label"), stats.get('forward_count', 0))
        ]

        # Add each stat with formatted numbers
        for label, value in stat_items:
            # Skip if value is 0
            if value == 0:
                continue

            # Create label and value
            ttk.Label(stats_frame, text=f"{label}:", anchor=tk.E, width=12).grid(
                row=row, column=0, sticky=tk.W, pady=5)
            ttk.Label(stats_frame, text=format_number(value), anchor=tk.W).grid(
                row=row, column=1, sticky=tk.W, pady=5, padx=10)
            row += 1

        # If no stats were added, show a message
        if row == 0:
            ttk.Label(stats_frame, text=self.translator.translate("user_tab", "no_statistics_available")).pack(pady=20)

    def _fill_author_tab(self, parent, video_info):
        """Fill the author tab with user information

        Args:
            parent: The parent frame
            video_info: The video information dictionary
        """
        # Create frame for author info
        author_frame = ttk.Frame(parent)
        author_frame.pack(fill=tk.BOTH, expand=True)

        # Get author info
        author = video_info.get('author', {})

        if not author:
            ttk.Label(author_frame, text=self.translator.translate("user_tab", "no_author_information")).pack(pady=20)
            return

        # Add author details
        row = 0

        # Define author items to display
        author_items = [
            (self.translator.translate("user_tab", "username_label"), author.get('nickname', self.translator.translate("user_tab", "unknown"))),
            (self.translator.translate("user_tab", "uid_label"), author.get('uid', self.translator.translate("user_tab", "unknown"))),
            (self.translator.translate("user_tab", "secuserid_label"), author.get('sec_uid', self.translator.translate("user_tab", "unknown"))),
            (self.translator.translate("user_tab", "followers_label"), format_number(author.get('follower_count', 0))),
            (self.translator.translate("user_tab", "following_label"), format_number(author.get('following_count', 0))),
            (self.translator.translate("user_tab", "total_likes_label"), format_number(author.get('total_favorited', 0)))
        ]

        # Add each author detail
        for label, value in author_items:
            ttk.Label(author_frame, text=f"{label}", anchor=tk.E, width=12).grid(
                row=row, column=0, sticky=tk.W, pady=5)
            ttk.Label(author_frame, text=str(value), anchor=tk.W).grid(
                row=row, column=1, sticky=tk.W, pady=5, padx=10)
            row += 1

        # Add signature if available
        if 'signature' in author and author['signature']:
            ttk.Label(author_frame, text=self.translator.translate("user_tab", "signature_label"), anchor=tk.E, width=12).grid(
                row=row, column=0, sticky=tk.NW, pady=5)

            # Use Text widget for multi-line signature
            signature_text = tk.Text(author_frame, height=3, width=30, wrap=tk.WORD)
            signature_text.grid(row=row, column=1, sticky=tk.W, pady=5, padx=10)
            signature_text.insert(tk.END, author['signature'])
            signature_text.config(state=tk.DISABLED)  # Make read-only

    def _download_single_video(self, video_info):
        """Download a single video from the details dialog

        Args:
            video_info: The video information dictionary
        """
        if not self.app.client.is_configured:
            messagebox.showwarning(
                self.translator.translate("user_tab", "warning_title"),
                self.translator.translate("user_tab", "setup_api_key_first")
            )
            return

        # Check if download is already in progress
        if self.downloading:
            messagebox.showinfo(
                self.translator.translate("user_tab", "info_title"),
                self.translator.translate("user_tab", "download_in_progress")
            )
            return

        # Set download flag
        self.downloading = True

        # Get settings
        settings = {}
        if hasattr(self.app, 'settings_tab'):
            settings = self.app.settings_tab.get_settings()

        # Create downloader with current settings
        downloader = VideoDownloader(
            self.app.download_path,
            use_description=settings.get('rename_with_desc', False),
            skip_existing=settings.get('skip_existing', True),
            postprocess_enabled=settings.get('postprocess_enabled', False),
            trim_start_seconds=settings.get('postprocess_trim_start_seconds', 1.5),
            mirror_video=settings.get('postprocess_mirror_video', True),
            delete_original_after_processing=settings.get('postprocess_delete_original', False),
            ffmpeg_path=settings.get('ffmpeg_path'),
            subtitle_cover_enabled=settings.get('subtitle_cover_enabled', False),
            subtitle_cover_workspace=settings.get('subtitle_cover_workspace'),
            subtitle_cover_detect_fps=settings.get('subtitle_cover_detect_fps', 1.5),
            subtitle_cover_mode=settings.get('subtitle_cover_mode', 'blur'),
            subtitle_ai_workspace=settings.get('subtitle_ai_workspace'),
            subtitle_ai_auto_detect=settings.get('subtitle_ai_auto_detect', True),
            subtitle_ai_require_cuda=settings.get('subtitle_ai_require_cuda', True),
            subtitle_ai_quality=settings.get('subtitle_ai_quality', 18)
        )

        # Prepare user folder
        user_folder = self._prepare_user_folder()

        # Determine media type
        media_type_codes = {
            0: 'video', 2: 'image', 4: 'video', 68: 'image',
            51: 'video', 55: 'video', 58: 'video', 61: 'video', 150: 'image'
        }
        media_type = media_type_codes.get(video_info.get('aweme_type', 0), 'video')

        # Update status
        aweme_id = video_info.get('aweme_id', self.translator.translate("user_tab", "unknown"))
        download_type = self.translator.translate("user_tab", "media_type_video" if media_type == 'video' else "media_type_image")
        self.status_var.set(f"{self.translator.translate('user_tab', 'downloading_media')} {download_type}: {aweme_id}")
        self.app.main_window.update_status(f"{self.translator.translate('user_tab', 'downloading_media')} {download_type}: {aweme_id}")

        # Start download in background thread
        self.app.run_in_thread(
            self._download_single_thread,
            self._on_single_download_complete,
            video_info=video_info,
            downloader=downloader,
            user_folder=user_folder,
            media_type=media_type
        )

    def _download_single_thread(self, video_info, downloader, user_folder, media_type):
        """Background thread function for downloading a single video/image

        Args:
            video_info: The video information dictionary
            downloader: VideoDownloader instance
            user_folder: Folder to save to
            media_type: Type of media (video/image)

        Returns:
            tuple: (success, file_path)
        """
        try:
            # Prepare URL based on media type
            if media_type == 'video':
                play_url = self._get_play_url_from_video(video_info)
                if not play_url:
                    return False, None

                # Structure data for downloader
                structured_data = {
                    'id': video_info.get('aweme_id', ''),
                    'desc': video_info.get('desc', ''),
                    'author_name': video_info.get('author', {}).get('nickname', ''),
                    'author_id': video_info.get('author', {}).get('uid', ''),
                    'platform': self.user_platform,
                    'media_type': 'video',
                    'create_time': video_info.get('create_time', ''),
                    'video_urls': play_url if isinstance(play_url, list) else [play_url]
                }

            else:  # Image or album
                image_urls = self._get_image_url_from_video(video_info)
                if not image_urls:
                    return False, None

                # Structure data for downloader
                structured_data = {
                    'id': video_info.get('aweme_id', ''),
                    'desc': video_info.get('desc', ''),
                    'author_name': video_info.get('author', {}).get('nickname', ''),
                    'author_id': video_info.get('author', {}).get('uid', ''),
                    'platform': self.user_platform,
                    'media_type': 'image',
                    'create_time': video_info.get('create_time', ''),
                    'image_urls': image_urls
                }

            # Define progress callback (optional)
            def progress_callback(current, total):
                # No UI updates needed for single download
                pass

            # Download using main_downloader
            result = downloader.main_downloader(
                structured_data,
                output_dir=user_folder,
                progress_callback=progress_callback,
                max_retries=3
            )

            # Check result
            if result['success'] and result['files']:
                # Return the first file path (or only file path)
                warning_message = "\n".join(result['errors']) if result['errors'] else None
                return True, result['files'][0], warning_message
            else:
                return False, None, None

        except Exception as e:
            print(f"Error in single download thread: {e}")
            return False, None, None

    def _on_single_download_complete(self, result):
        """Handle completion of single download

        Args:
            result: (success, file_path) tuple
        """
        success, file_path, warning_message = result

        # Reset download flag
        self.downloading = False

        if success:
            self.status_var.set(self.translator.translate("user_tab", "download_complete"))

            # Ask if user wants to open the folder or file
            if os.path.isdir(file_path):
                # For album folders
                response = messagebox.askyesno(
                    self.translator.translate("user_tab", "download_complete"),
                    f"{self.translator.translate('user_tab', 'download_success')}\n\n{self.translator.translate('user_tab', 'open_folder_question')}",
                    icon="info"
                )
                if response:
                    open_folder(file_path)
            else:
                # For single files
                response = messagebox.askyesno(
                    self.translator.translate("user_tab", "download_complete"),
                    f"{self.translator.translate('user_tab', 'download_success')}\n\n{self.translator.translate('user_tab', 'open_folder_question')}",
                    icon="info"
                )
                if response:
                    open_folder(os.path.dirname(file_path))
            if warning_message:
                messagebox.showwarning(
                    self.translator.translate("user_tab", "warning_title"),
                    warning_message
                )
        else:
            self.status_var.set(self.translator.translate("user_tab", "download_failed"))
            messagebox.showerror(
                self.translator.translate("user_tab", "download_failed"),
                self.translator.translate("user_tab", "download_failed_message")
            )

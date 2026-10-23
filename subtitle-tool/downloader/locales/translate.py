"""
Translation module for TikHub Downloader
"""

import json
import os
import logging
import locale
import sys
import re
from pathlib import Path


class Translator:
    """
    Handles translations for the application
    """

    # Base mapping of common language codes to their display names
    # This serves as a fallback if a language file doesn't contain its own name
    BASE_LANGUAGE_MAP = {
        'en': 'English',
        'zh': '中文 (Chinese)',
        'fr': 'Français (French)',
        'de': 'Deutsch (German)',
        'es': 'Español (Spanish)',
        'it': 'Italiano (Italian)',
        'ja': '日本語 (Japanese)',
        'ko': '한국어 (Korean)',
        'ru': 'Русский (Russian)',
        'pt': 'Português (Portuguese)',
        'nl': 'Nederlands (Dutch)',
        'ar': 'العربية (Arabic)',
        'hi': 'हिन्दी (Hindi)',
        'tr': 'Türkçe (Turkish)',
        'vi': 'Tiếng Việt (Vietnamese)',
        'th': 'ไทย (Thai)'
    }

    def __init__(self, language='en'):
        """
        Initialize the translator

        Args:
            language (str): Language code (default: 'en')
        """
        self.language = language
        self.translations = {}
        self.available_languages = []
        self.language_map = {}  # Will store code -> display name mapping
        self.reverse_language_map = {}  # Will store display name -> code mapping

        # 设置日志级别，便于调试
        logging.basicConfig(level=logging.INFO)

        # Load languages and build the mappings
        self._load_languages()
        self._build_language_maps()

        # Load the specified language
        self.load_language(language)

        # 记录初始语言信息，便于调试
        logging.info(f"Translator initialized with language: {language}")
        logging.info(f"Available languages: {self.available_languages}")

    def _get_resource_path(self, relative_path):
        """
        获取资源的绝对路径，同时支持开发环境和打包后的环境

        Args:
            relative_path (str): 相对路径

        Returns:
            str: 资源的绝对路径
        """
        try:
            # 检查是否在PyInstaller打包的环境中
            if getattr(sys, '_MEIPASS', None):
                # PyInstaller打包后的临时目录
                base_path = sys._MEIPASS
                logging.info(f"Running in PyInstaller environment: {base_path}")
            else:
                # 开发环境
                # 如果是从downloader目录下的locales目录加载，需要向上一级
                script_dir = os.path.dirname(os.path.abspath(__file__))
                if os.path.basename(os.path.dirname(script_dir)) == 'downloader':
                    # 当前文件在 downloader/locales 目录，需要取 downloader 目录
                    base_path = os.path.dirname(script_dir)
                else:
                    # 当前文件直接在项目根目录下
                    base_path = script_dir
                logging.info(f"Running in development environment: {base_path}")

            # 构建完整路径
            full_path = os.path.join(base_path, relative_path)
            logging.info(f"Resource path resolved to: {full_path}")
            return full_path
        except Exception as e:
            logging.error(f"Error getting resource path: {str(e)}")
            # 在出错时尝试使用相对路径
            return relative_path

    def _load_languages(self):
        """
        Load available languages and extract information from language files
        """
        try:
            # 获取语言文件目录
            locale_dir = self._get_resource_path('locales')
            logging.info(f"Looking for language files in: {locale_dir}")

            # 确保目录存在
            if not os.path.exists(locale_dir):
                logging.warning(f"Locales directory does not exist: {locale_dir}")
                self.available_languages = ['en']  # 回退到英语
                return

            # 列出所有JSON文件
            if os.path.isdir(locale_dir):
                language_files = [os.path.join(locale_dir, f) for f in os.listdir(locale_dir)
                                 if f.endswith('.json')]
            else:
                language_files = []

            # 从文件名提取语言代码
            self.available_languages = [os.path.splitext(os.path.basename(file))[0]
                                       for file in language_files]

            if not self.available_languages:
                logging.warning(f"No language files found in {locale_dir}")
                self.available_languages = ['en']  # 回退到英语

            # 记录发现的语言
            logging.info(f"Discovered language files: {', '.join(self.available_languages)}")

        except Exception as e:
            logging.error(f"Error loading languages: {str(e)}")
            self.available_languages = ['en']  # 回退到英语

    def _build_language_maps(self):
        """
        Build mappings between language codes and display names
        Try to extract language name from each language file
        """
        # 初始化基本映射
        self.language_map = self.BASE_LANGUAGE_MAP.copy()

        # 获取locales目录
        locale_dir = self._get_resource_path('locales')

        # 对每种可用语言，尝试从文件中获取显示名称
        for lang_code in self.available_languages:
            try:
                # 如果基本映射中已有显示名称，则使用它
                if lang_code in self.language_map:
                    continue

                # 否则，尝试加载语言文件提取语言名称
                translation_file = os.path.join(locale_dir, f"{lang_code}.json")

                with open(translation_file, 'r', encoding='utf-8') as f:
                    lang_data = json.load(f)

                # 尝试从文件中获取语言名称
                if 'language_info' in lang_data and 'name' in lang_data['language_info']:
                    display_name = lang_data['language_info']['name']
                    native_name = lang_data['language_info'].get('native_name', display_name)

                    # 格式化为"本地名称 (英文名称)"，如果它们不同
                    if native_name != display_name:
                        self.language_map[lang_code] = f"{native_name} ({display_name})"
                    else:
                        self.language_map[lang_code] = display_name
                else:
                    # 如果找不到语言信息，则使用语言代码
                    self.language_map[lang_code] = lang_code.upper()

            except Exception as e:
                # 如果提取失败，使用语言代码作为名称
                logging.warning(f"Failed to extract language name for {lang_code}: {str(e)}")
                self.language_map[lang_code] = lang_code.upper()

        # 构建反向映射(显示名称 -> 代码)
        self.reverse_language_map = {name: code for code, name in self.language_map.items()}

        # 额外添加直接映射以增强健壮性
        for code in self.available_languages:
            self.reverse_language_map[code] = code

        # 记录语言映射
        logging.info(f"Language mappings: {self.language_map}")
        logging.info(f"Reverse language mappings: {self.reverse_language_map}")

    @staticmethod
    def detect_system_language():
        """
        Detect the system's default language

        Returns:
            str: Language code ('en', 'zh', etc.)
        """
        try:
            # Get system locale
            system_locale, _ = locale.getdefaultlocale()
            logging.info(f"Detected system locale: {system_locale}")

            # Default to English
            default_language = 'en'

            if system_locale:
                # Extract language code (first part before '_')
                lang_code = system_locale.split('_')[0].lower()
                logging.info(f"Extracted language code: {lang_code}")

                # Return the language code if it matches any known language
                if lang_code in Translator.BASE_LANGUAGE_MAP:
                    logging.info(f"Using detected language: {lang_code}")
                    return lang_code
                else:
                    logging.info(f"Language code {lang_code} not supported, using default: {default_language}")

            return default_language

        except Exception as e:
            logging.error(f"Error detecting system language: {str(e)}")
            return 'en'  # Fallback to English

    def load_language(self, language):
        """
        Load translations for the specified language

        Args:
            language (str): Language code
        """
        # 记录加载的语言，便于调试
        logging.info(f"Attempting to load language: {language}")

        if language not in self.available_languages:
            logging.warning(f"Language '{language}' not available, falling back to English")
            language = 'en'

        try:
            # 获取翻译文件路径
            locale_dir = self._get_resource_path('locales')
            translation_file = os.path.join(locale_dir, f"{language}.json")

            # 记录文件路径和是否存在
            logging.info(f"Loading language file: {translation_file}")
            logging.info(f"File exists: {os.path.exists(translation_file)}")

            # 列出目录中的所有文件（调试用）
            if os.path.isdir(os.path.dirname(translation_file)):
                logging.info(f"Files in directory: {os.listdir(os.path.dirname(translation_file))}")

            # 加载翻译
            with open(translation_file, 'r', encoding='utf-8') as f:
                self.translations = json.load(f)

            self.language = language
            logging.info(f"Successfully loaded language: {language}")
        except Exception as e:
            logging.error(f"Error loading language '{language}': {str(e)}")
            # 如果加载失败，尝试加载英语
            if language != 'en':
                self.load_language('en')

    def translate(self, module, key, **kwargs):
        """
        Translate a key from the specified module

        Args:
            module (str): Module name (e.g., 'settings_tab')
            key (str): Translation key
            kwargs: Format arguments for the translation

        Returns:
            str: Translated string or key if translation not found
        """
        try:
            # Get the module translations
            module_translations = self.translations.get(module, {})

            # Get the translation for the key
            translation = module_translations.get(key, key)

            # Apply format arguments if any
            if kwargs:
                translation = translation.format(**kwargs)

            return translation
        except Exception as e:
            logging.error(f"Translation error for {module}.{key}: {str(e)}")
            return key

    def get_available_languages(self):
        """
        Get a list of available languages

        Returns:
            list: List of language codes
        """
        return self.available_languages

    def get_language_names(self):
        """
        Get a list of language names for display in UI

        Returns:
            list: List of language names
        """
        # Return the display names for all available languages
        return [self.language_map.get(lang, lang) for lang in self.available_languages]

    def get_language_code_from_name(self, name):
        """
        Get language code from display name

        Args:
            name (str): Language display name

        Returns:
            str: Language code
        """
        # 添加调试信息
        logging.info(f"Attempting to get language code for name: '{name}'")
        logging.info(f"Available reverse mappings: {self.reverse_language_map}")

        # 尝试直接从反向映射获取
        code = self.reverse_language_map.get(name)
        if code:
            logging.info(f"Found language code '{code}' for name '{name}'")
            return code

        # 如果未找到，尝试查找最匹配的语言名称
        # 这增加了系统的健壮性，即使UI显示的名称与映射中的不完全匹配
        for display_name, lang_code in self.reverse_language_map.items():
            if name in display_name or display_name in name:
                logging.info(f"Found partial match: '{lang_code}' for '{name}'")
                return lang_code

        # 如果是语言代码本身，直接返回
        if name in self.available_languages:
            logging.info(f"Name '{name}' is a language code itself")
            return name

        # 默认返回英语
        logging.warning(f"Could not find language code for '{name}', defaulting to 'en'")
        return 'en'

    def reload_languages(self):
        """
        Reload all language information and mappings
        Useful after new language files are added at runtime
        """
        logging.info(f"Reloading languages (current: {self.language})")
        current_language = self.language
        self._load_languages()
        self._build_language_maps()
        self.load_language(current_language)
        return self.available_languages
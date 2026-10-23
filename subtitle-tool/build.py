import os
import shutil
import subprocess
import sys
import platform
import argparse
from typing import List, Tuple, Optional
from importlib import metadata

from downloader.constants import APP_VERSION


# 1️⃣ 配置和常量定义 (Configuration and constants)
class Config:
    """配置类，集中管理打包设置 (Configuration class for centralized build settings)"""
    # 获取当前Python解释器路径 (Get current Python interpreter path)
    PYTHON_PATH = sys.executable

    # 设置项目路径 (Set project paths)
    PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
    DOWNLOADER_DIR = os.path.join(PROJECT_DIR, "downloader")
    MAIN_SCRIPT = os.path.join(PROJECT_DIR, "main.py")

    # 当前操作系统 (Current operating system)
    CURRENT_OS = platform.system()

    # 应用程序名称 (Application name)
    EXE_NAME = f"TikHub_Downloader-{CURRENT_OS}-{APP_VERSION}"

    # 设置图标路径 (Set icon paths)
    ICON_PATHS = {
        "Windows": os.path.join(PROJECT_DIR, "downloader/assets/icon.ico"),
        "Darwin": os.path.join(PROJECT_DIR, "downloader/assets/icon.icns"),
        "Linux": os.path.join(PROJECT_DIR, "downloader/assets/icon.png")
    }

    # 项目子目录 (Project subdirectories)
    SUBDIRS = ["locales", "apis", "core", "ui", "utils", "assets"]

    # macOS特定设置 (macOS specific settings)
    MACOS_BUNDLE_ID = "com.tikhub.downloader"

    # 需要额外打包的文件或目录 (Additional files/directories to package)
    ADDITIONAL_DATA = [
        ('README.md', '.'),
        ('README-zh.md', '.'),
        ('LICENSE', '.'),
        ('LICENSE-VSR-APACHE-2.0.txt', '.'),
        ('THIRD_PARTY_NOTICES.md', '.'),
        # 添加其他可能需要的文件或目录
    ]


# 2️⃣ 辅助函数 (Helper functions)
def check_dependencies() -> bool:
    """
    检查所需依赖是否已安装 (Check if required dependencies are installed)

    Returns:
        bool: 是否所有依赖都已安装 (Whether all dependencies are installed)
    """
    required = ["PyInstaller", "setuptools"]
    missing = []

    for package in required:
        try:
            metadata.version(package)
            print(f"✓ 依赖已安装: {package} (Dependency installed)")
        except metadata.PackageNotFoundError:
            missing.append(package)

    if missing:
        print(f"❌ 缺少以下依赖: {', '.join(missing)} (Missing dependencies)")
        install = input("是否自动安装? (y/n) (Install automatically?): ")
        if install.lower() == 'y':
            try:
                for package in missing:
                    subprocess.check_call([Config.PYTHON_PATH, "-m", "pip", "install", package])
                    print(f"✓ 已安装 {package} (Installed)")
                return True
            except subprocess.CalledProcessError:
                print("❌ 依赖安装失败 (Failed to install dependencies)")
                return False
        else:
            return False

    return True


def get_icon_path(target_os: str) -> Optional[str]:
    """
    获取指定操作系统的图标路径 (Get icon path for specified OS)

    Args:
        target_os (str): 目标操作系统 (Target operating system)

    Returns:
        Optional[str]: 图标路径，如果不存在则为None (Icon path, or None if it doesn't exist)
    """
    icon_path = Config.ICON_PATHS.get(target_os)
    if icon_path and os.path.exists(icon_path):
        print(f"✓ 找到图标文件: {icon_path} (Icon file found)")
        return icon_path
    else:
        print(f"⚠️ 警告: {target_os}图标文件不存在，将使用默认图标 (Warning: Icon file not found)")
        return None


def clean_old_build() -> None:
    """清理旧的打包文件 (Clean old build files)"""
    print("🧹 开始清理旧的打包文件... (Cleaning old build files...)")

    # 删除旧的目录 (Delete old directories)
    for folder in ["build", "dist"]:
        path = os.path.join(Config.PROJECT_DIR, folder)
        if os.path.exists(path):
            shutil.rmtree(path)
            print(f"  ✓ 已删除: {folder} (Deleted)")

    # 删除可能存在的spec文件 (Delete possible spec files)
    spec_files = [
        os.path.join(Config.PROJECT_DIR, f"{os.path.basename(Config.MAIN_SCRIPT).replace('.py', '')}.spec"),
        os.path.join(Config.PROJECT_DIR, f"{Config.EXE_NAME}.spec")
    ]

    for spec_file in spec_files:
        if os.path.exists(spec_file):
            os.remove(spec_file)
            print(f"  ✓ 已删除: {os.path.basename(spec_file)} (Deleted)")

    print("✅ 清理完成 (Cleaning completed)")


def get_project_dirs() -> List[Tuple[str, str]]:
    """
    获取项目目录列表 (Get project directory list)

    Returns:
        List[Tuple[str, str]]: 源路径和目标路径的元组列表 (List of tuples of source and target paths)
    """
    # 存储找到的目录 (Store found directories)
    found_dirs = []

    # 检查downloader目录是否存在 (Check if downloader directory exists)
    if not os.path.exists(Config.DOWNLOADER_DIR):
        print(f"❌ 错误: 源码目录 {Config.DOWNLOADER_DIR} 不存在! (Error: Source directory doesn't exist)")
        return found_dirs

    # 检查并添加存在的目录 (Check and add existing directories)
    for dir_name in Config.SUBDIRS:
        dir_path = os.path.join(Config.DOWNLOADER_DIR, dir_name)
        if os.path.exists(dir_path) and os.path.isdir(dir_path):
            print(f"✓ 找到目录: {dir_name} (Directory found)")
            # 注意这里的目标路径应该是相对于EXE的路径 (Target path should be relative to the EXE)
            found_dirs.append((dir_path, dir_name))
        else:
            print(f"⚠️ 警告: 目录 {dir_name} 不存在，将被跳过 (Warning: Directory doesn't exist, will be skipped)")

    return found_dirs


# 3️⃣ 主打包函数 (Main build function)
def build_with_pyinstaller(target_os: str) -> bool:
    """
    使用PyInstaller打包 (Build with PyInstaller)

    Args:
        target_os (str): 目标操作系统 (Target operating system)

    Returns:
        bool: 打包是否成功 (Whether the build was successful)
    """
    print(f"📦 开始使用 PyInstaller 为 {target_os} 平台打包... (Starting to build for platform...)")

    # 检查是否在对应的平台上进行打包 (Check if building on the corresponding platform)
    if target_os != Config.CURRENT_OS:
        print(f"⚠️ 警告: 当前在 {Config.CURRENT_OS} 系统上为 {target_os} 平台打包 (Warning: Cross-platform building)")
        print("⚠️ 跨平台打包通常无法正常工作，特别是Windows到macOS (Cross-platform build usually doesn't work)")
        response = input("是否继续? (y/n) (Continue?): ")
        if response.lower() != 'y':
            print("❌ 已取消打包 (Build cancelled)")
            return False

    # 获取存在的项目目录 (Get existing project directories)
    dirs = get_project_dirs()
    if not dirs:
        print("❌ 错误: 未找到任何需要打包的目录! (Error: No directories found to package)")
        return False

    # 获取图标路径 (Get icon path)
    icon_path = get_icon_path(target_os)

    # 构造数据文件列表 (Construct data file list)
    datas = []

    # 添加项目目录 (Add project directories)
    for dir_path, dir_name in dirs:
        # 使用操作系统的路径分隔符 (Use OS path separator)
        datas.append(f"{dir_path}{os.pathsep}{dir_name}")

    # 添加额外的数据文件 (Add additional data files)
    for source, dest in Config.ADDITIONAL_DATA:
        full_source_path = os.path.join(Config.PROJECT_DIR, source)
        if os.path.exists(full_source_path):
            datas.append(f"{full_source_path}{os.pathsep}{dest}")

    # 基本PyInstaller命令 (Basic PyInstaller command)
    cmd = [
        Config.PYTHON_PATH, "-m", "PyInstaller",
        "--clean",  # 清理缓存
        "--onefile",
        "--name", Config.EXE_NAME,
    ]

    # 根据目标平台添加特定选项 (Add platform-specific options)
    if target_os == "Windows":
        # Windows下不显示控制台 (Don't show console on Windows)
        cmd.append("--windowed")
        if icon_path:
            cmd.extend(["--icon", icon_path])
    elif target_os == "Darwin":  # macOS
        # 重要修改：使用 --onedir 对于macOS应用程序更好
        # Crucial change: Use --onedir for better macOS application packaging
        cmd.remove("--onefile")
        cmd.append("--onedir")

        # 添加对 PyQt 和其他框架的支持 (Add support for PyQt and other frameworks)
        cmd.extend([
            "--add-binary", "/System/Library/Frameworks/Tk.framework/Tk:tk",
            "--add-binary", "/System/Library/Frameworks/Tcl.framework/Tcl:tcl",
        ])

        # 不显示终端 (Don't show terminal)
        cmd.append("--windowed")

        if icon_path:
            cmd.extend(["--icon", icon_path])

        # macOS特有选项 (macOS-specific options)
        cmd.extend([
            "--osx-bundle-identifier", Config.MACOS_BUNDLE_ID,
            # 支持通用二进制，支持 Intel 和 Apple Silicon
            "--target-architecture", "universal2"
        ])
    else:  # Linux
        # Linux下保留终端输出，方便调试 (Keep terminal output for debugging on Linux)
        if icon_path:
            cmd.extend(["--icon", icon_path])

    # 添加数据文件 (Add data files)
    for data in datas:
        cmd.extend(["--add-data", data])

    # 添加主脚本 (Add main script)
    cmd.append(Config.MAIN_SCRIPT)

    # 打印完整命令（便于调试）(Print full command for debugging)
    print("\n执行命令 (Executing command):")
    print(" ".join(cmd))
    print("")

    # 运行打包命令 (Run build command)
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        print(f"✅ {target_os} 平台打包完成 (Platform build completed)")

        # 显示生成的文件位置 (Show generated file location)
        dist_path = os.path.join(Config.PROJECT_DIR, "dist")
        if os.path.exists(dist_path):
            print(f"生成的文件位于: {dist_path} (Generated files located at)")
            files = os.listdir(dist_path)
            if files:
                print(f"文件列表: {', '.join(files)} (File list)")

        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ 打包失败: {e} (Build failed)")
        print(f"标准输出: {e.stdout}")
        print(f"标准错误: {e.stderr}")
        return False


# 4️⃣ 命令行参数处理 (Command line argument processing)
def parse_args():
    """
    解析命令行参数 (Parse command line arguments)

    Returns:
        argparse.Namespace: 解析后的参数 (Parsed arguments)
    """
    parser = argparse.ArgumentParser(description="TikHub Downloader 打包工具 (Packaging tool)")
    parser.add_argument(
        "--target", "-t",
        choices=["windows", "macos", "linux", "current"],
        help="打包目标平台 (Target platform for packaging)"
    )
    parser.add_argument(
        "--clean", "-c",
        action="store_true",
        help="仅清理旧的构建文件 (Only clean old build files)"
    )
    parser.add_argument(
        "--no-interactive", "-ni",
        action="store_true",
        help="非交互模式，使用当前系统作为目标平台 (Non-interactive mode, use current system as target platform)"
    )
    return parser.parse_args()


# 5️⃣ 主函数 (Main function)
def main():
    """主程序入口 (Main program entry)"""
    args = parse_args()

    # 检查主脚本是否存在 (Check if main script exists)
    if not os.path.exists(Config.MAIN_SCRIPT):
        print(f"❌ 错误: 主脚本 {Config.MAIN_SCRIPT} 不存在! (Error: Main script doesn't exist)")
        sys.exit(1)

    # 如果只需清理，则清理后退出 (If only cleaning, clean and exit)
    if args.clean:
        clean_old_build()
        print("🧹 清理完成，程序退出 (Cleaning completed, program exit)")
        return

    # 检查依赖 (Check dependencies)
    if not check_dependencies():
        print("❌ 缺少必要依赖，无法继续 (Missing necessary dependencies, cannot continue)")
        sys.exit(1)

    # 清理旧文件 (Clean old files)
    clean_old_build()

    # 显示操作系统信息 (Show OS information)
    print(f"当前操作系统: {Config.CURRENT_OS} (Current OS)")

    # 确定目标平台 (Determine target platform)
    target_platform = None

    # 如果指定了--target参数，使用指定的平台 (If --target is specified, use the specified platform)
    if args.target:
        if args.target == "windows":
            target_platform = "Windows"
        elif args.target == "macos":
            target_platform = "Darwin"  # macOS
        elif args.target == "linux":
            target_platform = "Linux"
        elif args.target == "current":
            target_platform = Config.CURRENT_OS

    # 如果指定了--no-interactive参数，使用当前平台 (If --no-interactive is specified, use current platform)
    if args.no_interactive and not target_platform:
        target_platform = Config.CURRENT_OS
        print(f"ℹ️ 非交互模式: 使用当前平台 {Config.CURRENT_OS} (Non-interactive mode: using current platform)")

    # 如果没有通过命令行指定平台且不是非交互模式，显示交互式菜单
    # (If no platform is specified and not in non-interactive mode, show interactive menu)
    if not target_platform:
        print("\n请选择打包目标平台 (Please select target platform):")
        print("1. Windows")
        print("2. macOS")
        print("3. Linux")
        print(f"4. 当前平台 ({Config.CURRENT_OS}) (Current platform)")

        try:
            choice = int(input("\n请输入选项编号 [1-4]: ").strip() or "4")

            if choice == 1:
                target_platform = "Windows"
            elif choice == 2:
                target_platform = "Darwin"  # macOS
            elif choice == 3:
                target_platform = "Linux"
            elif choice == 4:
                target_platform = Config.CURRENT_OS
            else:
                print(f"❌ 无效选项，默认使用当前平台 ({Config.CURRENT_OS}) (Invalid option, using current platform)")
                target_platform = Config.CURRENT_OS
        except ValueError:
            print(f"❌ 无效输入，默认使用当前平台 ({Config.CURRENT_OS}) (Invalid input, using current platform)")
            target_platform = Config.CURRENT_OS

    # 执行打包 (Execute packaging)
    success = build_with_pyinstaller(target_platform)

    if success:
        print("\n🚀 打包完成! (Packaging completed!)")

        # 根据不同平台显示不同的提示 (Display different prompts based on platform)
        if target_platform == "Windows":
            print("Windows可执行文件 (.exe) 已生成在 dist 目录中 (Windows executable generated in dist directory)")
        elif target_platform == "Darwin":
            print("macOS应用程序已生成在 dist 目录中 (macOS application generated in dist directory)")
        else:
            print("Linux可执行文件已生成在 dist 目录中 (Linux executable generated in dist directory)")
    else:
        print(
            "\n⚠️ 打包过程中遇到问题，请检查上面的错误信息 (Problems encountered during packaging, please check error messages above)")


if __name__ == "__main__":
    main()

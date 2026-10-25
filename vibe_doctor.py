import os
import sys
import subprocess
import shutil
import ast
import platform

# 常见 Python 标准库列表（用于过滤，防止把内置库当成第三方库去 pip install）
STDLIB_MODULES = {
    'os', 'sys', 'math', 'json', 'subprocess', 'shutil', 'ast', 'platform', 
    'time', 'datetime', 'pathlib', 'urllib', 'http', 'asyncio', 'logging', 
    're', 'collections', 'itertools', 'functools', 'typing', 'hashlib', 'base64'
}

def get_installed_packages():
    """获取当前环境中已安装的 pip 包"""
    try:
        output = subprocess.check_output([sys.executable, "-m", "pip", "list", "--format=freeze"], text=True)
        return {line.split('==')[0].lower() for line in output.splitlines() if '==' in line}
    except Exception:
        return set()

def scan_python_imports():
    """递归扫描当前目录下所有 Python 文件中的第三方 import"""
    third_party_deps = set()
    for root, _, files in os.walk("."):
        # 排除虚拟环境和隐藏目录
        if ".git" in root or "venv" in root or "__pycache__" in root or ".venv" in root:
            continue
        for file in files:
            if file.endswith(".py"):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read(), filename=file_path)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                mod_name = alias.name.split('.')[0]
                                if mod_name not in STDLIB_MODULES:
                                    third_party_deps.add(mod_name)
                        elif isinstance(node, ast.ImportFrom):
                            if node.level == 0 and node.module:
                                mod_name = node.module.split('.')[0]
                                if mod_name not in STDLIB_MODULES:
                                    third_party_deps.add(mod_name)
                except Exception as e:
                    pass
    return third_party_deps

def check_and_install_python_deps():
    print("🔍 [1/3] 正在扫描 Python 代码中的第三方依赖...")
    required_deps = scan_python_imports()
    if not required_deps:
        print("✨ 没有发现需要额外安装的 Python 第三方库。")
        return

    installed = get_installed_packages()
    missing_deps = [dep for dep in required_deps if dep.lower() not in installed]

    if not missing_deps:
        print("✅ 所有 Python 依赖均已满足。")
        return

    print("发现以下代码中引用但未安装的库: {missing_deps}")
    choice = input("🤔 是否自动执行 pip install 安装这些库？(y/n): ").strip().lower()
    if choice == 'y':
        for dep in missing_deps:
            print(f"📦 正在安装: {dep}...")
            subprocess.run([sys.executable, "-m", "pip", "install", dep])
        print("🎉 Python 依赖安装完成！")

def check_cli_tools():
    """跨平台检查并提示缺失的常用 CLI 工具"""
    print("🔍 [2/3] 正在检查基础 CLI 工具链...")
    tools = {
        "git": "请安装 Git",
        "gh": "GitHub CLI (建议安装以支持一键仓库克隆)",
        "just": "Just 任务运行器 (建议安装以支持统一工作流)"
    }
    
    os_type = platform.system()
    for tool, hint in tools.items():
        if not shutil.which(tool):
            print(f"⚠️ 警告: 未检测到工具 '{tool}' ({hint})")
            if os_type == "Darwin":
                print(f"   你可以运行: brew install {tool}")
            elif os_type == "Windows":
                print(f"   你可以运行: winget install {tool} 或 choco install {tool}")
            elif os_type == "Linux":
                print(f"   你可以运行: sudo apt install {tool}")
        else:
            print(f"✅ 已就绪: {tool}")

if __name__ == "__main__":
    print("🚀 启动 Vibe 智能环境诊断工具 (Env Doctor)...")
    check_cli_tools()
    check_and_install_python_deps()
    print("🎉 环境诊断与自动补全流程完毕！")
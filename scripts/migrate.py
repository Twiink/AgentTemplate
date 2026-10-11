"""兼容脚本入口；所有迁移诊断和执行逻辑由统一 CLI 负责。"""

from app.cli import migrate

if __name__ == "__main__":
    migrate()

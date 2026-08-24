"""命令行入口：``python -m xueqiu_mcp`` 或 ``xueqiu-mcp``。"""

from .server import run


def main() -> None:
    run()


if __name__ == "__main__":
    main()

"""源码检出环境的便捷入口；正式入口为 `nestra` console script。"""

from nestra.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

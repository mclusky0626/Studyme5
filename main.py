"""PyCharm 에서 Shift+F10 으로 바로 봇을 띄우기 위한 런처.

- 봇:      python main.py            (= python -m app.main)
- 관리 웹: python main.py admin      (= python -m app.admin)
"""

import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "admin":
        from app.admin.server import run as run_admin

        run_admin()
    else:
        from app.main import run

        run()

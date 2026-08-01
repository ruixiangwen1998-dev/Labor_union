import streamlit as st
import os
import importlib
import sys

# 將專案根目錄加入 Python 搜尋路徑，以利讀取 services
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
if PARENT_DIR not in sys.path:
    sys.path.append(PARENT_DIR)

from ui.nav_helper import NAV_KEY, apply_pending_navigation

st.set_page_config(page_title="Lobar Union 管理系統", layout="wide")

# 鎖定在同目錄下的 pages 資料夾
PAGES_DIR = os.path.join(CURRENT_DIR, "pages")

def load_pages():
    pages = {}
    if os.path.exists(PAGES_DIR):
        for file in sorted(os.listdir(PAGES_DIR)):
            if file.endswith(".py") and not file.startswith("_"):
                module_name = file[:-3]
                try:
                    full_module_name = f"ui.pages.{module_name}"
                    # 如果已經加載過該模組，使用 reload 強制刷新記憶體快取，對齊硬碟最新程式碼
                    if full_module_name in sys.modules:
                        mod = importlib.reload(sys.modules[full_module_name])
                    else:
                        mod = importlib.import_module(full_module_name)
                        
                    if hasattr(mod, "title") and hasattr(mod, "show"):
                        pages[mod.title] = mod.show
                except Exception as e:
                    st.sidebar.error(f"載入頁面 {file} 失敗: {e}")
    return pages

def main():
    st.sidebar.title("🧭 Lobar Union 系統導覽")
    pages = load_pages()
    
    if not pages:
        st.warning("請在 `ui/pages/` 目錄下新增頁面模組。")
        return
        
    page_titles = list(pages.keys())
    apply_pending_navigation()
    if NAV_KEY not in st.session_state or st.session_state[NAV_KEY] not in page_titles:
        st.session_state[NAV_KEY] = page_titles[0]
    choice = st.sidebar.radio("前往頁面", page_titles, key=NAV_KEY)

    # 執行該分頁的 show()
    pages[choice]()

if __name__ == "__main__":
    main()

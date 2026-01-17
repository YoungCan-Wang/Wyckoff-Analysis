import streamlit as st
from download_history import get_download_history


st.set_page_config(
    page_title="下载历史",
    page_icon="🕘",
    layout="wide",
)


st.title("🕘 下载历史（最近 10 条）")


def show_right_nav():
    style = """
    <style>
    .nav-wrapper {
        position: fixed;
        right: 20px;
        top: 50%;
        transform: translateY(-50%);
        z-index: 99999;
        display: flex;
        flex-direction: column;
        align-items: flex-end;
        gap: 8px;
    }

    .nav-toggle-checkbox {
        display: none;
    }

    .nav-content {
        background-color: var(--secondary-background-color);
        padding: 12px 8px;
        border-radius: 16px;
        box-shadow: 0 4px 20px rgba(0,0,0,0.1);
        display: flex;
        flex-direction: column;
        gap: 16px;
        border: 1px solid rgba(128, 128, 128, 0.2);
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        transform-origin: right center;
        opacity: 1;
        transform: translateX(0);
    }
    
    .nav-toggle-checkbox:not(:checked) ~ .nav-content {
        opacity: 0;
        transform: translateX(100px);
        pointer-events: none;
        height: 0;
        padding: 0;
        margin: 0;
        overflow: hidden;
    }

    .nav-toggle-btn {
        width: 24px;
        height: 24px;
        background-color: var(--secondary-background-color);
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        border: 1px solid rgba(128, 128, 128, 0.2);
        transition: all 0.3s ease;
        color: var(--text-color);
        font-size: 12px;
        user-select: none;
    }

    .nav-toggle-btn:hover {
        background-color: #FF4B4B;
        color: white;
        border-color: #FF4B4B;
    }
    
    .nav-toggle-checkbox:checked ~ .nav-toggle-btn .icon-collapse {
        display: inline-block;
    }
    .nav-toggle-checkbox:checked ~ .nav-toggle-btn .icon-expand {
        display: none;
    }
    
    .nav-toggle-checkbox:not(:checked) ~ .nav-toggle-btn .icon-collapse {
        display: none;
    }
    .nav-toggle-checkbox:not(:checked) ~ .nav-toggle-btn .icon-expand {
        display: inline-block;
    }
    
    .nav-item {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: 12px;
        background-color: var(--background-color);
        color: var(--text-color);
        text-decoration: none;
        transition: all 0.2s ease;
        font-size: 20px;
        border: 1px solid transparent;
    }
    
    .nav-item:hover {
        transform: scale(1.1);
        background-color: #FF4B4B;
        color: white;
        border-color: #FF4B4B;
        text-decoration: none;
    }
    
    .nav-item::after {
        content: attr(data-title);
        position: absolute;
        right: 60px;
        background: #333;
        color: white;
        padding: 4px 8px;
        border-radius: 4px;
        font-size: 12px;
        opacity: 0;
        visibility: hidden;
        transition: opacity 0.2s;
        white-space: nowrap;
        pointer-events: none;
    }
    
    .nav-item:hover::after {
        opacity: 1;
        visibility: visible;
    }
    </style>
    """

    content = """
    <div class="nav-wrapper">
        <input type="checkbox" id="nav-toggle" class="nav-toggle-checkbox" checked>
        
        <label for="nav-toggle" class="nav-toggle-btn" title="Toggle Navigation">
            <span class="icon-collapse">▶</span>
            <span class="icon-expand">◀</span>
        </label>
        
        <div class="nav-content">
            <a href="/" target="_self" class="nav-item" data-title="首页 Home">
                <span>🏠</span>
            </a>
            <a href="/CustomExport" target="_self" class="nav-item" data-title="自定义导出 Custom Export">
                <span>🧰</span>
            </a>
            <a href="/DownloadHistory" target="_self" class="nav-item" data-title="下载历史 Download History">
                <span>🕘</span>
            </a>
            <a href="/Changelog" target="_self" class="nav-item" data-title="更新日志 Changelog">
                <span>📢</span>
            </a>
            <a href="https://github.com/YoungCan-Wang/Wyckoff-Analysis" target="_blank" class="nav-item" data-title="辛苦各位点个star，欢迎提各种issue">
                <span>⭐</span>
            </a>
        </div>
    </div>
    """

    st.html(style + content)


show_right_nav()


history = get_download_history()
if not history:
    st.info("暂无下载记录。")
    st.stop()

rows = []
for item in history:
    rows.append(
        {
            "时间": item.get("ts", ""),
            "页面": item.get("page", ""),
            "数据源": item.get("source", ""),
            "文件名": item.get("file_name", ""),
            "大小(KB)": item.get("size_kb", 0),
        }
    )

st.dataframe(rows, use_container_width=True, height=320)

st.markdown("### 📥 重新下载")
for item in history:
    label = f"{item.get('ts','')} | {item.get('page','')} | {item.get('file_name','')}"
    st.download_button(
        label=label,
        data=item.get("data", b""),
        file_name=item.get("file_name", "download.bin"),
        mime=item.get("mime", "application/octet-stream"),
        use_container_width=True,
        key=f"rehit::{item.get('id','')}",
    )


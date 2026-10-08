import streamlit as st
import pandas as pd
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync
from bs4 import BeautifulSoup
import re
import datetime
import jpholiday
from collections import defaultdict
import os
import subprocess

# --- クラウド(Streamlit Cloud)環境用: Playwrightの自動インストール ---
@st.cache_resource
def install_playwright():
    os.system("playwright install chromium")
    os.system("playwright install-deps chromium")

install_playwright()

# --- 施設情報 ---
FACILITIES = {
    '3036': '森ノ宮キューズモール',
    '3453': 'セレッソフットサルパーク福島'
}

# --- ページ設定 ---
st.set_page_config(
    page_title="フットサルコート 空き状況一覧",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- カスタムCSS ---
custom_css = """
<style>
    #MainMenu, footer, header {visibility: hidden;}
    .page-header { text-align: center; padding: 20px 0; }
    .page-header h1 { font-size: 24px; font-weight: 700; margin-bottom: 8px; }
    .page-header p { font-size: 14px; color: #718096; }
    .result-panel {
        border-radius: 12px;
        padding: 16px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        margin-top: 20px;
        background-color: white;
    }
</style>
"""
st.markdown(custom_css, unsafe_allow_html=True)

# --- ページヘッダー ---
st.markdown("""
<div class="page-header">
    <h1>フットサルコート<br>空き状況一覧</h1>
    <p>募集が開始されている全期間（向こう約2ヶ月分）の「土日祝」かつ「2時間以上連続」で空いている枠を一覧表示します</p>
</div>
""", unsafe_allow_html=True)

# --- 検索パネル ---
st.markdown("### 検索対象施設")
st.markdown("- 森ノ宮キューズモール (屋根有コートのみ)\n- セレッソフットサルパーク福島")

# 施設を固定化
selected_codes = list(FACILITIES.keys())

st.write("") # 少し余白
search_clicked = st.button("全期間の空き状況を取得する", type="primary", use_container_width=True)

def parse_slot_text(raw_text, base_year):
    """テキストから「コート名」「日付」「時間帯」を抽出する"""
    text = re.sub(r'^.*?([A-Za-z0-9ア-ンあ-ん亜-熙])', r'\1', raw_text)
    pattern = r'^(.*?)\s+(\d{1,2})月(\d{1,2})日.*?[）\)]\s*(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$'
    match = re.search(pattern, text)
    if match:
        court = match.group(1)
        month = int(match.group(2))
        day = int(match.group(3))

        year = base_year
        if month == 1 and datetime.date.today().month >= 10:
            year += 1
        elif month == 2 and datetime.date.today().month >= 11:
            year += 1
        elif month == 3 and datetime.date.today().month == 12:
            year += 1

        try:
            date_obj = datetime.date(year, month, day)
        except ValueError:
            return None

        sh, sm = int(match.group(4)), int(match.group(5))
        eh, em = int(match.group(6)), int(match.group(7))
        return {
            'court': court,
            'date_obj': date_obj,
            'start_min': sh * 60 + sm,
            'end_min': eh * 60 + em
        }
    return None

def format_time(minutes):
    h = minutes // 60
    m = minutes % 60
    return f"{h:02d}:{m:02d}"

def fetch_facility_data_for_weeks(shop_code, weeks=10):
    """指定した施設の向こう数週間分(デフォルト10週間=約2ヶ月半)のデータを一括で取得する"""
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())

    raw_slots = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            # WAFに弾かれないよう標準的なブラウザとして偽装
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="ja-JP",
                timezone_id="Asia/Tokyo"
            )
            page = context.new_page()
            
            # ステルス化プラグインを適用
            stealth_sync(page)

            # 不要なリソースをブロックして高速化
            def intercept_route(route):
                if route.request.resource_type in ["image", "stylesheet", "media", "font"]:
                    route.abort()
                else:
                    route.continue_()
            page.route("**/*", intercept_route)

            import random
            import time
            for i in range(weeks):
                target_date = monday + datetime.timedelta(days=i * 7)
                date_str = f"{target_date.year}/{target_date.month}/{target_date.day}/"
                url = f"https://yoyaku.labola.jp/r/shop/{shop_code}/calendar_week/{date_str}"

                # ボット検知ブロックを回避するためのリトライ処理
                max_retries = 3
                for attempt in range(max_retries):
                    # アクセス前に人間らしい長めの待機時間を入れる
                    page.wait_for_timeout(random.randint(2000, 4000))
                    
                    page.goto(url, wait_until="networkidle", timeout=30000)
                    html = page.content()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    # カレンダーのテーブル枠自体が存在するかチェック（なければブロック画面とみなす）
                    if not soup.find('table'):
                        print(f"Blocked on {date_str}. Retrying... ({attempt+1}/{max_retries})")
                        page.wait_for_timeout(5000) # ブロックされたら5秒待って再試行
                        continue
                    
                    # 正常にHTMLが取得できたらループを抜けて解析へ
                    break

                slots = soup.find_all('td', class_='empty')

                for slot in slots:
                    # 正しいエスケープ: 実際の改行と空白を整理
                    raw_text = slot.text.strip()
                    raw_text = re.sub(r'\s+', ' ', raw_text)

                    parsed = parse_slot_text(raw_text, target_date.year)
                    if parsed:
                        # 屋根無のコート、およびパーティールームは対象外
                        if "屋根無" in parsed['court'] or "屋根なし" in parsed['court'] or "プレミアム・マルシェ・ロマン" in parsed['court']:
                            continue

                        dt = parsed['date_obj']
                        # 土日祝・今日以降のみ
                        if dt >= today and (dt.weekday() >= 5 or jpholiday.is_holiday(dt)):
                            parsed['facility'] = FACILITIES[shop_code]
                            raw_slots.append(parsed)

            browser.close()
    except Exception as e:
        st.error(f"{FACILITIES[shop_code]}のデータ取得中にエラーが発生しました: {e}")

    return raw_slots

def fetch_all_data(shop_codes):
    all_raw_slots = []

    for code in shop_codes:
        res = fetch_facility_data_for_weeks(code, weeks=10)
        all_raw_slots.extend(res)

    # 重複排除
    unique_slots = {}
    for s in all_raw_slots:
        key = (s['facility'], s['date_obj'], s['court'], s['start_min'])
        unique_slots[key] = s
    all_raw_slots = list(unique_slots.values())

    # --- 2時間連続枠の抽出と結合 ---
    grouped = defaultdict(list)
    for s in all_raw_slots:
        grouped[(s['facility'], s['date_obj'], s['court'])].append(s)

    valid_slots = []
    for key, group in grouped.items():
        group.sort(key=lambda x: x['start_min'])

        merged = []
        current = None
        for s in group:
            if current is None:
                current = dict(s)
            else:
                if current['end_min'] == s['start_min']:
                    current['end_min'] = s['end_min']
                else:
                    merged.append(current)
                    current = dict(s)
        if current:
            merged.append(current)

        for m in merged:
            duration = m['end_min'] - m['start_min']
            if duration >= 120:
                hours = duration / 60
                wd = ["月", "火", "水", "木", "金", "土", "日"][m['date_obj'].weekday()]
                holiday_name = jpholiday.is_holiday_name(m['date_obj'])
                if holiday_name:
                    wd = f"祝({wd})"

                valid_slots.append({
                    "施設": m['facility'],
                    "日付": f"{m['date_obj'].month}月{m['date_obj'].day}日（{wd}）",
                    "_sort_date": m['date_obj'].month * 100 + m['date_obj'].day,
                    "時間帯": f"{format_time(m['start_min'])}-{format_time(m['end_min'])}",
                    "連続時間": f"{hours:g}時間",
                    "コート": m['court']
                })

    return valid_slots

if search_clicked:
    if not selected_codes:
        st.warning("施設を1つ以上選択してください。")
    else:
        with st.spinner("募集が開始されている全期間（向こう約10週間分）のデータを取得・分析しています...（約1分ほどかかります）"):
            try:
                data = fetch_all_data(selected_codes)
            except Exception as e:
                st.error(f"取得処理でエラーが発生しました: {e}")
                st.stop()

            if data:
                df = pd.DataFrame(data)
                
                # デバッグ用：取得できた最新の日付を確認する
                max_date = df['_sort_date'].max()
                max_month = max_date // 100
                max_day = max_date % 100
                
                df = df.sort_values(by=['_sort_date', '時間帯', '施設']).drop(columns=['_sort_date'])

                st.success(f"向こう約2ヶ月半間で、{len(data)}件の「土日祝で2時間以上連続する空き枠」が見つかりました！")
                st.info(f"💡 【システム状況】システムが一番遠くまで取得できた日付は **{max_month}月{max_day}日** でした。もしこれより先の枠がある場合は範囲外です。")

                st.markdown('<div class="result-panel">', unsafe_allow_html=True)
                st.dataframe(df, use_container_width=True, hide_index=True)

                st.markdown("### 予約リンク")
                for code in selected_codes:
                    st.markdown(f"- [{FACILITIES[code]}](https://yoyaku.labola.jp/r/shop/{code}/calendar_week/)")
                st.markdown('</div>', unsafe_allow_html=True)

            else:
                st.info("募集が開始されている全期間において、土日祝に2時間以上連続する空き枠は見つかりませんでした。")


import streamlit as st
import pandas as pd

st.set_page_config(page_title="门店报表查询", layout="centered")

@st.cache_data
def load_permission():
    return pd.read_excel("permission.xlsx", dtype={"查看人员编号": str})

@st.cache_data
def load_all_data():
    xls = pd.ExcelFile("data.xlsx")
    sheet_data = {}
    for sheet in xls.sheet_names:
        df = xls.parse(sheet)
        sheet_data[sheet] = df
    return sheet_data

def main():
    st.title("📊 门店报表查询系统")
    st.markdown("请输入您的门店名称与编号，查看专属报表：")

    store_name = st.text_input("门店名称")
    viewer_id = st.text_input("查看人员编号（数字）")

    if store_name and viewer_id:
        permissions = load_permission()
        match = permissions[
            (permissions['门店名称'].astype(str).str.strip() == store_name.strip()) &
            (permissions['查看人员编号'].astype(str).str.strip() == viewer_id.strip())
        ]

        if not match.empty:
            all_data = load_all_data()
            found = False
            for sheet_name, df in all_data.items():
                if store_name in df.values:
                    found = True
                    st.success(f"✅ 成功验证权限，正在显示 {store_name} 的报表数据")
                    matched_rows = df[df.apply(lambda row: row.astype(str).str.contains(store_name), axis=1)]
                    st.dataframe(matched_rows)
                    break
            if not found:
                st.warning("⚠️ 在数据文件中未找到该门店的数据，请联系管理员。")
        else:
            st.error("❌ 无权限访问，请检查门店名称与编号是否正确。")

if __name__ == "__main__":
    main()

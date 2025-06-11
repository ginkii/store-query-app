import streamlit as st
import pandas as pd

st.set_page_config(page_title="门店金额查询", layout="centered")
st.title("🔍 门店金额查询系统")

# 上传权限表 & 数据文件
auth_file = st.file_uploader("请上传权限绑定文件（门店权限模板.xlsx）", type=["xlsx"], key="auth")
data_file = st.file_uploader("请上传门店数据文件（含多个 sheet）", type=["xlsx"], key="data")

store_input = st.text_input("请输入门店名称")
viewer_input = st.text_input("请输入查看人员编号（纯数字）")
submit = st.button("查询")

if auth_file and data_file and submit:
    try:
        auth_df = pd.read_excel(auth_file)
        auth_dict = dict(zip(auth_df["门店名称"], auth_df["查看人员编号"].astype(str)))

        if store_input not in auth_dict:
            st.error("❌ 未找到该门店的权限记录")
        elif auth_dict[store_input] != viewer_input:
            st.error("⚠️ 查看人员编号不匹配，无权限查看该门店数据")
        else:
            # 匹配通过，开始遍历数据文件的 sheet
            xls = pd.ExcelFile(data_file)
            matched_df = None

            for sheet in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name=sheet, header=None)
                if store_input in df.astype(str).values:
                    matched_df = df
                    break

            if matched_df is not None:
                st.success(f"✅ 门店【{{store_input}}】数据已加载：")
                st.dataframe(matched_df, use_container_width=True)
            else:
                st.error("❌ 数据文件中未找到匹配的门店表")
    except Exception as e:
        st.error(f"读取或解析出错：{{e}}")

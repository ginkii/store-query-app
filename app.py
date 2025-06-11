import streamlit as st
import pandas as pd

st.set_page_config(page_title="门店金额查询", layout="centered")
st.title("🔍 门店金额查询系统（权限精确 + 数据内容模糊）")

auth_file = st.file_uploader("请上传权限绑定文件（门店权限模板.xlsx）", type=["xlsx"], key="auth")
data_file = st.file_uploader("请上传门店数据文件（含多个 sheet）", type=["xlsx"], key="data")

store_input = st.text_input("请输入完整门店名称（与权限表一致）")
viewer_input = st.text_input("请输入查看人员编号（纯数字）")
submit = st.button("查询")

if auth_file and data_file and submit:
    try:
        auth_df = pd.read_excel(auth_file)
        auth_dict = dict(zip(auth_df["门店名称"], auth_df["查看人员编号"].astype(str)))

        if store_input not in auth_dict:
            st.error("❌ 未找到该门店的权限记录")
        elif auth_dict[store_input] != viewer_input:
            st.error("⚠️ 编号不匹配，无权限查看该门店数据")
        else:
            # 遍历每个 sheet，查找内容中是否包含门店名
            xls = pd.ExcelFile(data_file)
            matched_df = None
            for sheet in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name=sheet, header=None).astype(str)
                if df.apply(lambda col: col.str.contains(store_input, na=False)).any().any():
                    matched_df = df
                    break

            if matched_df is not None:
                st.success(f"✅ 在文件中成功匹配包含“{store_input}”内容的门店数据")
                st.dataframe(matched_df, use_container_width=True)
            else:
                st.error("❌ 数据文件中未找到包含该门店名称的内容")
    except Exception as e:
        st.error(f"发生错误：{e}")

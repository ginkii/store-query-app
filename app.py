# streamlit_app.py - 门店报表系统 (含后台汇总下载功能)
"""
门店报表查询系统
新增功能: 线下成本数据持久化存储、后台一键下载分门店汇总Excel
"""

import streamlit as st
import pandas as pd
import numpy as np
import pymongo
from pymongo import MongoClient
import gridfs
import re
from datetime import datetime
from typing import Dict, Any
import base64
import io
import os

# ==========================================
# 1. 常量定义 (完整硬编码元数据字典)
# ==========================================
REPORT_META_MAP = {
    # === 利润表 Items ===
    "1、线上毛利": {"seq": 1, "comment": "计费基准项。还原所有利润与费用后的综合获利基数。"},
    "1、回款": {"seq": 1, "comment": "核心流入基准。门店本期实际入账的总金额。"},
    "--利润项": {"seq": 2, "comment": "经营总盘子。反映本月线上业务产生的各类收入及补贴总额。"},
    "--收入项": {"seq": 2, "comment": "资金流入项。"},
    "------ 牵牛花毛利": {"seq": 3, "comment": "即手机牵牛花看到的毛利。已扣除配送费、佣金、商品成本"},
    "------订单款": {"seq": 3, "comment": "基础营业流水。门店结算款扣除平台内支出。"},
    "------ 企客返款": {"seq": 4, "comment": "针对上个月的企业配送优惠的实际到账补贴。"},
    "------企客待返款": {"seq": 5, "comment": "本周期内已产生、但尚未到账的预估补贴"},
    "------其他返款": {"seq": 6, "comment": "平台其他的奖励金额"},
    "2、经营费用": {"seq": 7, "comment": "线上运营产生的相关费用。"},
    "--营销推广": {"seq": 8, "comment": "用于提升流量和转化的推广支出。"},
    "------美团推广": {"seq": 9, "comment": "美团平台的推广通、金牛等付费推广支出。"},
    "------京东推广": {"seq": 10, "comment": "京东到家平台的营销推广费用。"},
    "--综合成本": {"seq": 11, "comment": "运营过程中的其他必要成本。"},
    "------人工工资": {"seq": 18, "comment": "实际归属于当月的工资"},
    "------仓库房租": {"seq": 19, "comment": "实际归属于当月的房租"},
    "------物业水电": {"seq": 20, "comment": "实际归属于当月的物业费"},
    "------耗材成本": {"seq": 22, "comment": "实际归属于当月的耗材费"},
    "--损耗成本": {"seq": 23, "comment": "运营过程中的损耗及差异。"},
    "------采收损耗": {"seq": 23, "comment": "实际归属于当月的售后费"},
    "------仓内损耗": {"seq": 24, "comment": "实际归属于当月的物流费"},
    "------售后损耗": {"seq": 25, "comment": "实际归属于当月的税金"}, 
    "--其他费用": {"seq": 26, "comment": "实际归属于当月的其他费用"},
    "3、线下支出": {"seq": 16, "comment": "门店硬性开支。汇总了所有线下实体经营产生的现金支出。"},
    "------人工工资支出": {"seq": 17, "comment": "当月实际发放工资，包含绩效、福利费、奖金"},
    "------仓库房租支出": {"seq": 18, "comment": "本月实际支付给房东的仓库或店面租金。"},
    "------物业水电支出": {"seq": 19, "comment": "本月实付的物业管理费、清扫费及保安费等。"},
    "------耗材成本支出": {"seq": 21, "comment": "本月实际支付出去的应用耗材费"},
    "------其他支出": {"seq": 99, "comment": "门店发生的其他杂项现金支出。"},
    "净利润": {"seq": 999, "comment": "最终经营成果。计算公式：线上毛利 - 经营费用 - 线下支出。"},
}

# 页面配置
if "page_configured" not in st.session_state:
    st.set_page_config(
        page_title="门店报表系统",
        page_icon="🏪",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    st.session_state.page_configured = True

# ==========================================
# 2. 核心类与数据库管理 (升级版)
# ==========================================

class ConfigManager:
    @staticmethod
    def get_mongodb_config():
        try:
            if hasattr(st, 'secrets') and 'mongodb' in st.secrets:
                return st.secrets["mongodb"]
        except Exception:
            pass
        return {
            'uri': os.getenv('MONGODB_URI', 'mongodb://localhost:27017/'),
            'database_name': 'store_report_db'
        }

class MongoDBManager:
    def __init__(self):
        self.config = ConfigManager.get_mongodb_config()
        self.client = None
        self.db = None
        self.fs = None
        self._connect()

    def _connect(self):
        try:
            self.client = MongoClient(self.config['uri'], serverSelectionTimeoutMS=2000)
            self.db = self.client[self.config['database_name']]
            self.fs = gridfs.GridFS(self.db)
            # 初始化 offline_costs 集合
            if "offline_costs" not in self.db.list_collection_names():
                self.db.create_collection("offline_costs")
        except Exception as e:
            print(f"MongoDB连接失败: {e}")
            self.client = None

    def is_connected(self):
        return self.client is not None

    def save_guide_pdf(self, file_obj):
        """保存指引PDF"""
        if not self.fs: return False
        try:
            old_file = self.fs.find_one({"filename": "guide.pdf"})
            if old_file: self.fs.delete(old_file._id)
            self.fs.put(file_obj, filename="guide.pdf")
            return True
        except Exception as e:
            st.error(f"上传失败: {e}")
            return False

    def get_guide_pdf(self):
        if not self.fs: return None
        try:
            return self.fs.find_one({"filename": "guide.pdf"})
        except:
            return None
            
    def check_admin_password(self, password):
        return password == "admin888" 

    # --- 新增：线下成本数据管理 ---
    
    def save_offline_cost(self, store_id: str, month: str, cost_data: dict):
        """保存线下成本记录 (覆盖式更新：同一门店同一月份只留一条最新记录)"""
        if not self.db: return False
        try:
            collection = self.db["offline_costs"]
            record = {
                "store_id": store_id,
                "month": month,
                "data": cost_data,
                "updated_at": datetime.now()
            }
            # 更新或插入 (Upsert)
            collection.update_one(
                {"store_id": store_id, "month": month}, 
                {"$set": record}, 
                upsert=True
            )
            return True
        except Exception as e:
            st.error(f"数据保存失败: {e}")
            return False

    def get_all_offline_costs(self):
        """获取所有线下成本数据 (用于后台下载)"""
        if not self.db: return []
        try:
            return list(self.db["offline_costs"].find({}, {"_id": 0}).sort("store_id", 1))
        except Exception:
            return []

@st.cache_resource
def get_db_manager():
    return MongoDBManager()

# ==========================================
# 3. 样式处理 (Styler)
# ==========================================

def style_dataframe(df: pd.DataFrame, table_type: str = "profit"):
    # (样式代码保持不变，为节省篇幅略去重复部分，与上一版本一致...)
    # ... 请确保包含 row_style_logic, formatter 等逻辑 ...
    
    # 这里为了代码运行完整性，简写核心逻辑
    numeric_cols = [c for c in df.columns if c not in ['费项', '注释', '序号']]
    styler = df.style.format({col: "{:,.2f}" for col in numeric_cols}, na_rep="-")
    if '序号' in df.columns:
        styler = styler.format({'序号': "{:.0f}"})

    def row_style_logic(row):
        item_name = str(row['费项']).strip()
        bg, fc, fw, fs, bd = "white", "black", "normal", "normal", ""
        
        if "净利润" in item_name:
            bg, fc, fw, bd = "#D4EDDA", "#D9534F", "bold", "2px solid #333"
        elif item_name.startswith("1、"):
            bg, fc, fw = "#F2F2F2", "#000000", "bold"
        elif item_name.startswith("--") and not item_name.startswith("------"):
            fc, fw, fs = "#333333", "bold", "italic"
        elif item_name.startswith("------"):
            fc = "#666666"
        elif re.match(r'^\d+、', item_name):
            bg, fw = "#F2F2F2", "bold"

        css = f"background-color: {bg}; color: {fc}; font-weight: {fw}; font-style: {fs};"
        if bd: css += f"border-top: {bd}; border-bottom: {bd};"
        return [css] * len(row)

    styler = styler.apply(row_style_logic, axis=1)
    
    styler = styler.applymap(lambda x: "min-width: 200px; text-align: left;", subset=['费项'])
    if '注释' in df.columns:
        styler = styler.applymap(lambda x: "color: #888888; font-style: italic; font-size: 0.9em; min-width: 250px; white-space: normal; text-align: left;", subset=['注释'])
    if '序号' in df.columns:
        styler = styler.applymap(lambda x: "text-align: center; width: 50px; color: #555;", subset=['序号'])

    header_bg = "#E8F0FE" if table_type == "profit" else "#E6FFFA"
    styler = styler.set_table_styles([
        {'selector': 'th', 'props': [('background-color', header_bg), ('font-weight', 'bold'), ('text-align', 'center')]}
    ])
    return styler

# ==========================================
# 4. 数据处理逻辑
# ==========================================

def get_base64_of_bin_file(bin_file):
    data = bin_file.read()
    return base64.b64encode(data).decode()

def generate_mock_data(store_id):
    """生成模拟数据"""
    # ... (保持原样，生成 10月-1月 的模拟数据) ...
    items = [
        "1、线上毛利", "--利润项", "------ 牵牛花毛利", "------ 企客返款", 
        "2、经营费用", "--营销推广", "------美团推广", 
        "--综合成本", "------人工工资", "------仓库房租",
        "3、线下支出", "------人工工资支出", "------仓库房租支出", "------物业水电支出", 
        "------耗材成本支出", "------其他支出"
    ]
    months = ["10月", "11月", "12月", "1月"] 
    data = {"费项": items}
    for m in months:
        col_data = []
        for item in items:
            if "1、" in item: val = 60000.0
            else: val = 0.0 if ("--" in item and "------" not in item) else np.random.uniform(500, 5000)
            col_data.append(val)
        data[m] = col_data
    return pd.DataFrame(data)

def process_report_data(df: pd.DataFrame, offline_costs: Dict[str, float]) -> pd.DataFrame:
    """注入成本并计算净利润"""
    month_cols = [c for c in df.columns if c not in ['费项', '注释', '序号']]
    if not month_cols: return df
    current_month = month_cols[-1] # 最新月
    
    # 注入线下成本 (仅最新月)
    cost_mapping = {
        "------人工工资支出": offline_costs.get('wages', 0),
        "------仓库房租支出": offline_costs.get('rent', 0),
        "------物业水电支出": offline_costs.get('utilities', 0),
        "------耗材成本支出": offline_costs.get('consumables', 0),
        "------其他支出": offline_costs.get('others', 0)
    }
    for item_name, cost_val in cost_mapping.items():
        if item_name in df['费项'].values:
            df.loc[df['费项'] == item_name, current_month] = cost_val

    # 计算净利润逻辑 ... (同上个版本)
    try:
        val_online = df.loc[df['费项']=="1、线上毛利", month_cols].astype(float).values[0] if "1、线上毛利" in df['费项'].values else 0
        val_exp = df.loc[df['费项']=="2、经营费用", month_cols].astype(float).values[0] if "2、经营费用" in df['费项'].values else 0
        
        # 计算线下支出总额 (简单求和)
        offline_items = list(cost_mapping.keys())
        val_offline = df.loc[df['费项'].isin(offline_items), month_cols].sum().values
        # 更新 3、线下支出 行
        if "3、线下支出" in df['费项'].values:
            df.loc[df['费项'] == "3、线下支出", month_cols] = val_offline

        net_profit = val_online - val_exp - val_offline
        
        if "净利润" in df['费项'].values:
            df.loc[df['费项'] == "净利润", month_cols] = net_profit
        else:
            new_row = {"费项": "净利润"}
            for idx, c in enumerate(month_cols): new_row[c] = net_profit[idx]
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    except: pass
    
    return df

def add_meta_columns(df: pd.DataFrame) -> pd.DataFrame:
    comments, seqs = [], []
    for item in df['费项']:
        meta = REPORT_META_MAP.get(str(item).strip(), {})
        comments.append(meta.get("comment", ""))
        seqs.append(meta.get("seq", np.nan))
    df['注释'] = comments
    df['序号'] = seqs
    cols = list(df.columns)
    fixed = ['费项', '注释', '序号']
    return df[fixed + [c for c in cols if c not in fixed]]

# ==========================================
# 5. 功能模块 (含升级后的后台下载)
# ==========================================

def render_query_system(db_manager):
    st.header("📊 门店经营报表")
    if 'query_stage' not in st.session_state: st.session_state.query_stage = 'input'
    
    # 1. 查询
    c1, c2 = st.columns([3, 1])
    with c1: qid = st.text_input("请输入门店编号", key="qid")
    with c2: 
        st.write(""); st.write("")
        if st.button("查询", type="primary"):
            st.session_state.current_store_id = qid; st.session_state.query_stage = 'form'; st.rerun()

    # 2. 录入 & 提交 (新增保存逻辑)
    if st.session_state.query_stage == 'form':
        st.info(f"📍 当前门店: {st.session_state.current_store_id}")
        with st.form("cost"):
            st.write("请录入本期线下成本：")
            c1, c2, c3 = st.columns(3)
            with c1: 
                wages = st.number_input("人工工资支出", step=100.0)
                rent = st.number_input("仓库房租支出", step=100.0)
            with c2: 
                util = st.number_input("物业水电支出", step=100.0)
                cons = st.number_input("耗材成本支出", step=50.0)
            with c3: 
                others = st.number_input("其他支出", step=50.0)
            
            if st.form_submit_button("提交并生成报表", type="primary"):
                # 1. 暂存 Session
                cost_dict = {"wages": wages, "rent": rent, "utilities": util, "consumables": cons, "others": others}
                st.session_state.offline_costs = cost_dict
                
                # 2. 永久保存到 MongoDB (新增)
                # 获取当前月份 (这里模拟获取，实际应从 MockData 逻辑一致处获取，或默认为"本月")
                # 为了简化，我们假设是 "1月" (与 generate_mock_data 一致)
                current_month = "1月" 
                db_manager.save_offline_cost(st.session_state.current_store_id, current_month, cost_dict)
                
                st.session_state.query_stage = 'report'
                st.rerun()

    # 3. 报表展示 (同前)
    if st.session_state.query_stage == 'report':
        df = generate_mock_data(st.session_state.current_store_id)
        df = process_report_data(df, st.session_state.offline_costs)
        df = add_meta_columns(df)
        
        # PDF Link
        pdf = db_manager.get_guide_pdf()
        if pdf:
            b64 = get_base64_of_bin_file(pdf)
            st.markdown(f'<a href="data:application/pdf;base64,{b64}" download="指引.pdf">📄 下载报表指引</a>', unsafe_allow_html=True)
            
        t1, t2 = st.tabs(["利润表", "现金表"])
        with t1: st.dataframe(style_dataframe(df, "profit"), use_container_width=True, hide_index=True)
        with t2: st.dataframe(style_dataframe(df.copy(), "cash"), use_container_width=True, hide_index=True) # 简化copy
        
        if st.button("重新查询"): st.session_state.query_stage = 'input'; st.rerun()

def render_admin_system(db_manager):
    """后台管理：含文件上传 + 数据下载"""
    st.header("🔐 后台管理系统")
    pwd = st.text_input("管理员密码", type="password")
    
    if db_manager.check_admin_password(pwd):
        st.success("验证通过")
        tab1, tab2 = st.tabs(["📄 财务指引管理", "📥 线下成本汇总下载"])
        
        # Tab 1: PDF 上传
        with tab1:
            st.info("上传新的财务报表指引PDF，将覆盖旧文件。")
            up_file = st.file_uploader("选择PDF", type="pdf")
            if up_file and st.button("上传指引"):
                if db_manager.save_guide_pdf(up_file): st.success("✅ 上传成功")
        
        # Tab 2: 汇总下载 (新增功能)
        with tab2:
            st.markdown("### 导出各门店线下成本明细")
            st.write("点击下方按钮，将生成一个Excel文件。**每个门店将作为一个独立的Sheet页**，列出该门店的历史填报记录。")
            
            if st.button("生成汇总报表"):
                # 1. 获取所有数据
                all_records = db_manager.get_all_offline_costs()
                
                if not all_records:
                    st.warning("暂无任何提交记录。")
                else:
                    # 2. 转换为 DataFrame
                    # 扁平化数据结构: store_id, month, wages, rent ... updated_at
                    flat_data = []
                    for r in all_records:
                        row = {
                            "门店编号": r.get("store_id"),
                            "月份": r.get("month"),
                            "提交时间": r.get("updated_at"),
                            "人工工资": r.get("data", {}).get("wages", 0),
                            "仓库房租": r.get("data", {}).get("rent", 0),
                            "物业水电": r.get("data", {}).get("utilities", 0),
                            "耗材成本": r.get("data", {}).get("consumables", 0),
                            "其他支出": r.get("data", {}).get("others", 0)
                        }
                        flat_data.append(row)
                    
                    df_all = pd.DataFrame(flat_data)
                    
                    # 3. 写入 Excel (分 Sheet)
                    output = io.BytesIO()
                    # 使用 xlsxwriter 引擎 (Streamlit 默认环境通常支持，如报错需 pip install XlsxWriter)
                    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                        # 获取唯一门店列表
                        stores = df_all["门店编号"].unique()
                        for store in stores:
                            # 筛选该门店数据
                            store_df = df_all[df_all["门店编号"] == store]
                            # 写入 Sheet, 名称为门店ID
                            store_df.to_excel(writer, sheet_name=str(store), index=False)
                            
                    output.seek(0)
                    
                    # 4. 提供下载按钮
                    st.download_button(
                        label="📥 点击下载 Excel 汇总表",
                        data=output,
                        file_name=f"线下成本汇总_{datetime.now().strftime('%Y%m%d')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )

def main():
    with st.sidebar:
        st.title("🏪 门店系统")
        mod = st.selectbox("功能", ["门店查询系统", "批量上传系统", "权限管理系统"])
        db = get_db_manager()
        
    if mod == "门店查询系统": render_query_system(db)
    elif mod == "权限管理系统": render_admin_system(db)
    else: st.info("批量上传模块")

if __name__ == "__main__":
    main()

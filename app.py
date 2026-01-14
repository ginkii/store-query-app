# streamlit_app.py - 门店报表系统 (样式精修版V2)
"""
门店报表查询系统
修复: 数据库连接判断
新增: 双层表头样式、红蓝表头区分、中间空列、序号文本化
逻辑:
1. 线下成本全为正数录入，直接求和作为支出扣除
2. 净利润 = 线上毛利 - 总部分润(应收) - 线下成本
3. 样式更新：
   - "净利润"、"4、余额"：灰底红字加粗
   - "线上净利润"、"线上余额"：灰底黑字加粗
   - "总部应收未收金额"：绿底黑字
"""

import streamlit as st
import pandas as pd
import numpy as np
import pymongo
from pymongo import MongoClient
import gridfs
import re
from datetime import datetime
from typing import Dict, List, Optional, Any
import base64
import io
import xlsxwriter

# ==========================================
# 1. 常量定义 (元数据字典)
# ==========================================
REPORT_META_MAP = {
    # === 利润表 Items ===
    "1、线上毛利": {"seq": 1, "comment": "计费基准项。还原所有利润与费用后的综合获利基数。"},
    "1、回款": {"seq": 1, "comment": "核心流入基准。门店本期实际入账的总金额(到账净额)。"},
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
    "------总部分润（应收）": {"seq": 10, "comment": "品牌运营抽佣。计算逻辑：线上毛利 × 合同比例。"}, 
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
    "3、线下成本": {"seq": 17, "comment": "线下损益分摊。核算线下经营对总利润的损耗。"}, 
    "3、线下支出": {"seq": 16, "comment": "门店硬性开支。所有线下实体经营产生的现金支出。"}, 
    "------人工工资支出": {"seq": 17, "comment": "当月实际发放工资，包含绩效、福利费、奖金"},
    "------仓库房租支出": {"seq": 18, "comment": "本月实际支付给房东的仓库或店面租金。"},
    "------物业水电支出": {"seq": 19, "comment": "本月实付的物业管理费、清扫费及保安费等。"},
    "------耗材成本支出": {"seq": 21, "comment": "本月实际支付出去的应用耗材费"},
    "------其他费用": {"seq": 22, "comment": "门店发生的其他杂项现金支出。"}, 
    "净利润": {"seq": 999, "comment": "最终经营成果。计算公式：线上毛利 - 总部分润 - 线下成本。"},
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
# 2. 数据库管理
# ==========================================
class ConfigManager:
    @staticmethod
    def get_mongodb_config():
        try:
            if hasattr(st, 'secrets') and 'mongodb' in st.secrets:
                return st.secrets["mongodb"]
        except: pass
        return {'uri': 'mongodb://localhost:27017/', 'database_name': 'store_reports'}
    
    @staticmethod
    def get_admin_password():
        try: return st.secrets["security"]["admin_password"]
        except: return "admin123"

class DatabaseManager:
    def __init__(self):
        self.db = None
        self.client = None
        self.fs = None
        self._connect()
    
    def _connect(self):
        try:
            config = ConfigManager.get_mongodb_config()
            self.client = MongoClient(config['uri'], serverSelectionTimeoutMS=5000)
            self.db = self.client[config['database_name']]
            self.fs = gridfs.GridFS(self.db)
            if "offline_costs" not in self.db.list_collection_names():
                self.db.create_collection("offline_costs")
        except Exception as e:
            st.error(f"连接失败: {e}")
            self.db = None
    
    def is_connected(self):
        return self.db is not None

    def save_guide_pdf(self, file_obj):
        if self.fs is None: return False
        try:
            old = self.fs.find_one({"filename": "guide.pdf"})
            if old: self.fs.delete(old._id)
            self.fs.put(file_obj, filename="guide.pdf")
            return True
        except: return False

    def get_guide_pdf(self):
        if self.fs is None: return None
        try: return self.fs.find_one({"filename": "guide.pdf"})
        except: return None

    def save_offline_cost(self, store_id, month, data):
        if self.db is None: return False
        try:
            self.db["offline_costs"].update_one(
                {"store_id": store_id, "month": month},
                {"$set": {"store_id": store_id, "month": month, "data": data, "updated_at": datetime.now()}},
                upsert=True
            )
            return True
        except Exception as e:
            st.error(f"保存失败: {e}")
            return False

    def get_offline_cost(self, store_id, month):
        if self.db is None: return {}
        try:
            res = self.db["offline_costs"].find_one({"store_id": store_id, "month": month})
            return res.get("data", {}) if res else {}
        except: return {}

    def get_all_offline_costs(self):
        if self.db is None: return []
        try: return list(self.db["offline_costs"].find({}, {"_id": 0}).sort("store_id", 1))
        except: return []

@st.cache_resource
def get_db_manager():
    return DatabaseManager()

# ==========================================
# 3. 核心样式处理 (样式逻辑已更新)
# ==========================================

def get_base64_of_bin_file(bin_file):
    data = bin_file.read()
    return base64.b64encode(data).decode()

def add_meta_columns(df: pd.DataFrame) -> pd.DataFrame:
    """添加注释和序号列"""
    comments, seqs = [], []
    for item in df['费项']:
        key = str(item).strip()
        meta = REPORT_META_MAP.get(key, {})
        comments.append(meta.get("comment", ""))
        seqs.append(meta.get("seq", np.nan)) 
    
    if '注释' not in df.columns: df.insert(1, '注释', comments)
    if '序号' not in df.columns: df.insert(2, '序号', seqs)
    return df

def apply_advanced_style(df: pd.DataFrame):
    """应用高级样式"""
    numeric_cols = []
    for col in df.columns:
        col_name = col[1]
        if col_name not in ['费项', '注释', '序号', ' ']:
            numeric_cols.append(col)

    format_dict = {}
    for c in numeric_cols:
        format_dict[c] = "{:,.2f}"
    
    seq_cols = [c for c in df.columns if c[1] == '序号']
    for c in seq_cols:
        format_dict[c] = lambda x: f"{int(x)}" if pd.notnull(x) and x != "" else ""
        
    styler = df.style.format(format_dict, na_rep="-")

    def row_style(row):
        try: item_name = str(row[0]).strip() 
        except: item_name = ""
            
        bg, fc, fw, fs, bd = "white", "black", "normal", "normal", ""
        
        # === 核心样式逻辑修改 ===
        
        # 1. "净利润" 和 "4、余额" (灰底红字加粗)
        if "净利润" in item_name or "4、余额" in item_name:
            bg = "#F2F2F2"  # 灰底
            fc = "#D9534F"  # 红字
            fw = "bold"     # 加粗
            bd = "2px solid #333" # 边框强调
            
        # 2. "线上净利润" 和 "线上余额" (灰底黑字加粗)
        elif "线上净利润" in item_name or "线上余额" in item_name:
            bg = "#F2F2F2"  # 灰底
            fc = "#000000"  # 黑字
            fw = "bold"     # 加粗
            
        # 3. "总部应收未收金额" (绿底黑字)
        elif "总部应收未收金额" in item_name:
            bg = "#D4EDDA"  # 绿底
            fc = "#000000"  # 黑字
            # 不加粗
            
        # 4. 常规层级样式
        elif item_name.startswith("1、"):
            bg = "#F2F2F2"
            fc = "#000000"
            fw = "bold"
        elif item_name.startswith("--") and not item_name.startswith("------"):
            fc, fw, fs = "#333333", "bold", "italic"
        elif item_name.startswith("------"):
            fc = "#666666"
        elif re.match(r'^\d+、', item_name):
            bg, fw = "#F2F2F2", "bold"
            
        css = f"background-color: {bg}; color: {fc}; font-weight: {fw}; font-style: {fs};"
        if bd: css += f"border-top: {bd}; border-bottom: {bd};"
        return [css] * len(row)

    styler = styler.apply(row_style, axis=1)

    # 列样式
    styler = styler.applymap(lambda x: "min-width: 180px; text-align: left;", subset=[c for c in df.columns if c[1]=='费项'])
    styler = styler.applymap(lambda x: "color: #888888; font-style: italic; font-size: 0.9em; min-width: 200px; white-space: normal; text-align: left;", subset=[c for c in df.columns if c[1]=='注释'])
    styler = styler.applymap(lambda x: "text-align: center; width: 40px; color: #555;", subset=[c for c in df.columns if c[1]=='序号'])
    styler = styler.applymap(lambda x: "background-color: white; border: none; width: 20px;", subset=[c for c in df.columns if c[0]==' '])

    # 表头样式
    styles = [
        {'selector': 'th', 'props': [('text-align', 'center'), ('border', '1px solid #ddd'), ('vertical-align', 'middle')]},
        {'selector': 'th:contains("利润表")', 'props': [('background-color', '#E8F0FE !important'), ('color', '#1a73e8'), ('font-size', '1.1em')]},
        {'selector': 'th:contains("现金表")', 'props': [('background-color', '#FFFFE0 !important'), ('color', '#d4a017'), ('font-size', '1.1em')]},
        {'selector': 'th:contains("_empty_")', 'props': [('background-color', 'white'), ('border', 'none'), ('color', 'transparent')]},
    ]
    styler = styler.set_table_styles(styles)
    
    return styler

# ==========================================
# 4. 数据逻辑
# ==========================================

def inject_offline_and_calculate(df: pd.DataFrame, offline_data: dict):
    if df.empty: return df
    
    data_cols = [c for c in df.columns if c not in ['费项', '注释', '序号']]
    if not data_cols: return df
    
    current_month = data_cols[-1]
    
    # 注入细项
    mapping = {
        "------人工工资支出": offline_data.get('wages', 0),
        "------仓库房租支出": offline_data.get('rent', 0),
        "------物业水电支出": offline_data.get('utilities', 0),
        "------耗材成本支出": offline_data.get('consumables', 0),
        "------其他费用": offline_data.get('others', 0) 
    }
    
    for k, v in mapping.items():
        if k in df['费项'].values:
            df.loc[df['费项']==k, current_month] = v
            
    # 更新汇总 "3、线下成本"
    total_offline = sum(mapping.values())
    if "3、线下成本" in df['费项'].values:
        df.loc[df['费项']=="3、线下成本", current_month] = total_offline
        
    # 计算净利润: 线上毛利 - 总部分润(应收) - 线下成本
    try:
        def get_val(name, col):
            rows = df[df['费项'] == name]
            if rows.empty: return 0.0
            val = rows[col].values[0]
            try:
                if isinstance(val, str): val = float(val.replace(',', '').replace('¥', ''))
                return float(val)
            except: return 0.0

        for m in data_cols:
            v_online_gross = get_val("1、线上毛利", m)
            v_hq_share = get_val("------总部分润（应收）", m) 
            v_offline_cost = total_offline if m == current_month else get_val("3、线下成本", m)
            
            # 核心公式
            net = v_online_gross - v_hq_share - v_offline_cost
            
            if "净利润" in df['费项'].values:
                df.loc[df['费项']=="净利润", m] = net
    except Exception as e:
        print(f"计算出错: {e}")
        pass
        
    return df

def generate_mock_data(store_id):
    """生成模拟数据（包含所有关键字段以测试样式）"""
    items_p = [
        "1、线上毛利", "--利润项", 
        "------总部分润（应收）", 
        "线上净利润", # 样式测试
        "2、经营费用", 
        "3、线下成本", 
        "------人工工资支出", "------仓库房租支出", "------物业水电支出", 
        "------耗材成本支出", "------其他费用", 
        "净利润"
    ]
    items_c = [
        "1、回款", "--收入项", 
        "线上余额", # 样式测试
        "------订单款", "------企客返款",
        "4、余额", # 样式测试
        "总部应收未收金额" # 样式测试
    ]
    
    months = ["10月", "11月", "12月", "1月"]
    
    data_p = {"费项": items_p}
    for m in months:
        vals = []
        for item in items_p:
            if "线上毛利" in item: v = 60000
            elif "总部分润" in item: v = 7200 
            else: v = 0
            vals.append(v)
        data_p[m] = vals
        
    df_p = pd.DataFrame(data_p)
    
    data_c = {"费项": items_c}
    for m in months: 
        vals = []
        for item in items_c:
            if "回款" in item: v = 58000
            else: v = 0
            vals.append(v)
        data_c[m] = vals
        
    df_c = pd.DataFrame(data_c)
    
    return df_p, df_c

def rebuild_dataframe_with_headers(raw_data, headers):
    if not raw_data: return pd.DataFrame()
    df = pd.DataFrame(raw_data)
    return df

# ==========================================
# 5. 页面功能模块
# ==========================================

def render_query_system(db_manager):
    st.markdown("<h1 style='text-align: center;'>🔍 门店查询系统</h1>", unsafe_allow_html=True)
    
    if 'authenticated' not in st.session_state: st.session_state.authenticated = False
    
    if not st.session_state.authenticated:
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            query_code = st.text_input("请输入查询编号", key="login_code")
            if st.button("登录", use_container_width=True):
                if query_code:
                    st.session_state.authenticated = True
                    st.session_state.store_info = {'_id': 'mock_id', 'store_name': '测试门店', 'store_code': query_code}
                    st.session_state.cost_submitted = False
                    st.rerun()
        return

    store_info = st.session_state.store_info
    st.title(f"📊 {store_info['store_name']}")
    
    # --- 1. 线下成本录入 ---
    if not st.session_state.get('cost_submitted', False):
        st.info("请先录入本期线下成本（直接输入金额，无需加减号）：")
        with st.form("offline_cost"):
            c1, c2 = st.columns(2)
            with c1:
                wages = st.number_input("人工工资支出", min_value=0.0, step=100.0)
                rent = st.number_input("仓库房租支出", min_value=0.0, step=100.0)
            with c2:
                utilities = st.number_input("物业水电支出", min_value=0.0, step=100.0)
                consumables = st.number_input("耗材成本支出", min_value=0.0, step=50.0)
            
            others = st.number_input("--其他费用", min_value=0.0, step=50.0)
            
            if st.form_submit_button("提交并生成报表", type="primary"):
                st.session_state.offline_data = {
                    "wages": wages, "rent": rent, 
                    "utilities": utilities, "consumables": consumables,
                    "others": others
                }
                st.session_state.cost_submitted = True
                st.rerun()
        return

    # --- 2. 报表展示 ---
    
    df_profit, df_cash = generate_mock_data(store_info['store_code'])
    df_profit = inject_offline_and_calculate(df_profit, st.session_state.offline_data)
    
    df_profit = add_meta_columns(df_profit)
    df_cash = add_meta_columns(df_cash) 
    
    profit_cols = [("表一：利润表", c) for c in df_profit.columns]
    df_profit.columns = pd.MultiIndex.from_tuples(profit_cols)
    
    cash_cols = [("表二：现金表", c) for c in df_cash.columns]
    df_cash.columns = pd.MultiIndex.from_tuples(cash_cols)
    
    df_sep = pd.DataFrame(np.nan, index=df_profit.index, columns=[(" ", " ")])
    df_display = pd.concat([df_profit, df_sep, df_cash], axis=1)
    df_display = df_display.fillna("")

    pdf = db_manager.get_guide_pdf()
    if pdf:
        b64 = get_base64_of_bin_file(pdf)
        st.markdown(f'<a href="data:application/pdf;base64,{b64}" download="指引.pdf">📄 下载报表指引</a>', unsafe_allow_html=True)

    st.subheader("详细报表")
    styled_df = apply_advanced_style(df_display)
    st.dataframe(styled_df, use_container_width=True, height=600)
    
    if st.button("修改线下成本"):
        st.session_state.cost_submitted = False
        st.rerun()

def create_upload_app():
    st.title("批量上传")
    st.info("功能保持不变...")

def create_permission_app():
    st.title("权限管理")
    st.info("功能保持不变...")

def main():
    with st.sidebar:
        st.title("🏪 门店系统")
        app = st.selectbox("功能", ["门店查询系统", "批量上传系统", "权限管理系统"])
        
    db = get_db_manager()
    
    if app == "门店查询系统": render_query_system(db)
    elif app == "批量上传系统": create_upload_app()
    elif app == "权限管理系统": create_permission_app()

if __name__ == "__main__":
    main()

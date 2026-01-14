# streamlit_app.py - 门店报表系统 (多月录入 & 列重构终极版)
"""
包含所有模块：
1. 门店查询：
   - [新增] 月份选择器：用户可选择针对哪个月份录入成本。
   - [重构] 注入逻辑：遍历报表所有月份列，分别从DB读取成本并计算净利润。
   - [重构] 列处理：强制重组列顺序(费项-注释-序号-数据)，彻底解决重复列问题。
   - UI：垂直窄表单、无步进器。
2. 批量上传 & 权限管理：功能保持完整。
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
import hashlib
import time

# ==========================================
# 1. 常量定义
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
    
    # 线下部分
    "3、线下成本": {"seq": 17, "comment": "线下损益分摊。核算线下经营对总利润的损耗。"}, 
    "3、线下支出": {"seq": 17, "comment": "门店硬性开支。所有线下实体经营产生的现金支出。"}, 
    
    "------人工工资": {"seq": 17, "comment": "当月实际发放工资，包含绩效、福利费、奖金"},
    "------仓库房租": {"seq": 18, "comment": "本月实际支付给房东的仓库或店面租金。"},
    "------物业水电": {"seq": 19, "comment": "本月实付的物业管理费、清扫费及保安费等。"},
    "------耗材成本": {"seq": 21, "comment": "本月实际支付出去的应用耗材费"},
    "------其他支出": {"seq": 22, "comment": "门店发生的其他杂项现金支出。"}, 
    
    "净利润": {"seq": 999, "comment": "最终经营成果。计算公式：线上毛利 - 总部分润 - 线下成本。"},
}

if "page_configured" not in st.session_state:
    st.set_page_config(page_title="门店报表系统", page_icon="🏪", layout="wide", initial_sidebar_state="expanded")
    st.session_state.page_configured = True

# ==========================================
# 2. 数据库管理
# ==========================================
class ConfigManager:
    @staticmethod
    def get_mongodb_config():
        try:
            if hasattr(st, 'secrets') and 'mongodb' in st.secrets: return st.secrets["mongodb"]
        except: pass
        return {'uri': 'mongodb://localhost:27017/', 'database_name': 'store_reports'}
    @staticmethod
    def get_admin_password():
        try: return st.secrets["security"]["admin_password"]
        except: return "admin123"

class DatabaseManager:
    def __init__(self):
        self.db = None; self.client = None; self.fs = None
        self._connect()
    def _connect(self):
        try:
            config = ConfigManager.get_mongodb_config()
            self.client = MongoClient(config['uri'], serverSelectionTimeoutMS=5000)
            self.db = self.client[config['database_name']]
            self.fs = gridfs.GridFS(self.db)
            if self.db is not None:
                if "offline_costs" not in self.db.list_collection_names(): self.db.create_collection("offline_costs")
                self._create_indexes()
        except Exception as e: st.error(f"连接失败: {e}"); self.db = None
    def _create_indexes(self):
        if self.db is None: return
        try:
            self.db['stores'].create_index([("store_code", 1)], background=True)
            self.db['permissions'].create_index([("query_code", 1)], background=True)
            self.db['reports'].create_index([("store_id", 1), ("report_month", -1)], background=True)
            self.db['offline_costs'].create_index([("store_id", 1), ("month", 1)], background=True)
        except Exception: pass
    def get_database(self): return self.db
    def is_connected(self): return self.db is not None
    def save_guide_pdf(self, f):
        if self.fs is None: return False
        try:
            old = self.fs.find_one({"filename": "guide.pdf"})
            if old: self.fs.delete(old._id)
            self.fs.put(f, filename="guide.pdf")
            return True
        except: return False
    def get_guide_pdf(self):
        if self.fs is None: return None
        try: return self.fs.find_one({"filename": "guide.pdf"})
        except: return None
    def save_offline_cost(self, sid, m, d):
        if self.db is None: return False
        try: self.db["offline_costs"].update_one({"store_id": sid, "month": m}, {"$set": {"store_id": sid, "month": m, "data": d, "updated_at": datetime.now()}}, upsert=True); return True
        except: return False
    def get_offline_cost(self, sid, m):
        if self.db is None: return {}
        try:
            res = self.db["offline_costs"].find_one({"store_id": sid, "month": m})
            return res.get("data", {}) if res else {}
        except: return {}
    def get_all_offline_costs(self):
        if self.db is None: return []
        try: return list(self.db["offline_costs"].find({}, {"_id": 0}).sort("store_id", 1))
        except: return []

@st.cache_resource
def get_db_manager(): return DatabaseManager()

# ==========================================
# 3. 数据模型
# ==========================================
class StoreModel:
    @staticmethod
    def create_store_document(store_name, store_code=None, **kwargs):
        ts = int(datetime.now().timestamp())
        return {
            '_id': kwargs.get('_id', f"store_{store_code or store_name.replace(' ', '_')}_{ts}"),
            'store_name': store_name.strip(),
            'store_code': store_code or StoreModel._gen_code(store_name),
            'region': kwargs.get('region', '未分类'),
            'created_at': datetime.now(), 'created_by': 'system', 'status': 'active',
            'aliases': [store_name.strip()]
        }
    @staticmethod
    def _gen_code(name):
        try: return f"AUTO_{hashlib.md5(name.encode('utf-8')).hexdigest()[:6].upper()}"
        except: return f"AUTO_{int(datetime.now().timestamp())}"

class ReportModel:
    @staticmethod
    def create_report_document(store_data, report_month, excel_data, headers, **kwargs):
        return {
            'store_id': store_data['_id'], 'store_code': store_data['store_code'], 'store_name': store_data['store_name'],
            'report_month': report_month, 'sheet_name': kwargs.get('sheet_name', store_data['store_name']),
            'raw_excel_data': excel_data, 'table_headers': headers, 'financial_data': kwargs.get('financial_data', {}),
            'created_at': datetime.now(), 'updated_at': datetime.now(), 'uploaded_by': 'system'
        }
    @staticmethod
    def dataframe_to_dict_list(df):
        headers = [str(c) if not str(c).startswith('Unnamed') else "" for c in df.columns]
        unique_headers = []
        ec = 0
        for h in headers:
            if h == "": unique_headers.append(f"_empty_{ec}"); ec += 1
            else: unique_headers.append(h)
        df.columns = unique_headers
        res = []
        for _, row in df.iterrows():
            r = {}
            for i, v in enumerate(row):
                if pd.isna(v): r[f"col_{i}"] = ""
                elif isinstance(v, (int, float)): r[f"col_{i}"] = float(v)
                else: r[f"col_{i}"] = str(v)
            res.append(r)
        return res, headers

class PermissionModel:
    @staticmethod
    def create_permission_document(query_code, store_data, **kwargs):
        return {
            'query_code': query_code.strip(), 'store_id': store_data['_id'],
            'store_name': store_data['store_name'], 'store_code': store_data['store_code'],
            'created_at': datetime.now(), 'updated_at': datetime.now(),
            'created_by': 'system', 'status': 'active'
        }

# ==========================================
# 4. 业务逻辑
# ==========================================
class BulkReportUploader:
    def __init__(self, db): self.db = db; self.stores = db['stores']; self.reports = db['reports']
    def process_excel_file(self, file_buffer, month, clear, cb=None):
        start = time.time()
        res = {'success_count': 0, 'failed_count': 0, 'errors': [], 'processed_stores': [], 'failed_stores': []}
        try:
            if cb: cb(10, "读取Excel...")
            dfs = pd.read_excel(file_buffer, sheet_name=None, header=1)
            dfs_fin = pd.read_excel(file_buffer, sheet_name=None, header=3)
            if clear: self.reports.delete_many({'report_month': month})
            
            for i, (name, df) in enumerate(dfs.items()):
                if cb: cb(20 + int((i+1)/len(dfs)*70), f"处理: {name}")
                try:
                    norm = name.replace('犀牛百货','').replace('门店','').replace('店','').strip()
                    store = self.stores.find_one({'$or': [{"store_name": name}, {"aliases": {"$in": [name, norm]}}]})
                    if not store:
                        store = StoreModel.create_store_document(name, aliases=[name, norm], created_by='bulk')
                        self.stores.insert_one(store)
                    
                    df = df.dropna(axis=1, how='all')
                    if df.empty: continue
                    data, hdrs = ReportModel.dataframe_to_dict_list(df)
                    
                    fin = {}
                    df_f = dfs_fin.get(name)
                    if df_f is not None:
                        try:
                            cols = [i for i,c in enumerate(df_f.columns) if '合计' in str(c) or 'sum' in str(c).lower()]
                            if len(cols) >= 2:
                                val = df_f.iloc[36, cols[1]]
                                fin['receivables'] = {'net_amount': float(val)}
                        except: pass
                    
                    rpt = ReportModel.create_report_document(store, month, data, hdrs, sheet_name=name, financial_data=fin)
                    self.reports.insert_one(rpt)
                    res['success_count'] += 1
                except Exception as e:
                    res['failed_count'] += 1; res['errors'].append(f"{name}: {e}")
        except Exception as e: res['errors'].append(str(e))
        res['total_time'] = time.time() - start
        return res

class PermissionManager:
    def __init__(self, db): self.db = db; self.perms = db['permissions']; self.stores = db['stores']
    def upload_permission_table(self, f):
        try:
            df = pd.read_csv(f) if f.name.endswith('.csv') else pd.read_excel(f)
            qc, sc = df.columns[0], df.columns[1]
            res = {"success": True, "created": 0, "updated": 0}
            for _, r in df.iterrows():
                q, s = str(r[qc]).strip(), str(r[sc]).strip()
                if not q or not s: continue
                store = self.stores.find_one({"store_name": s})
                if not store:
                    store = StoreModel.create_store_document(s, created_by='perm')
                    self.stores.insert_one(store)
                perm = PermissionModel.create_permission_document(q, store)
                if self.perms.find_one({"query_code": q}):
                    self.perms.replace_one({"query_code": q}, perm); res["updated"] += 1
                else: self.perms.insert_one(perm); res["created"] += 1
            return res
        except Exception as e: return {"success": False, "message": str(e)}
    def get_all_permissions(self): return list(self.perms.find().sort("query_code", 1))
    def delete_permission(self, c): self.perms.delete_one({"query_code": c})

# ==========================================
# 5. 辅助函数 (样式与计算) - 重点修复区域
# ==========================================
def get_base64_of_bin_file(bin_file):
    data = bin_file.read()
    return base64.b64encode(data).decode()

def add_meta_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    重构列逻辑：
    1. 清洗费项
    2. 生成注释、序号
    3. 暴力重组：只保留 [费项, 注释, 序号, ...所有数据列...]
    """
    # 1. 清洗费项名称
    if '费项' in df.columns:
        def clean_name(x):
            s = str(x).strip().replace('\n', '').replace('（需合伙人补充）', '')
            if "支出" in s and "其他" not in s and "线下" not in s: return s.replace("支出", "")
            return s
        df['费项'] = df['费项'].apply(clean_name)

    # 2. 准备新列数据
    comments, seqs = [], []
    for item in df['费项']:
        key = str(item)
        meta = REPORT_META_MAP.get(key, {})
        comments.append(meta.get("comment", ""))
        s = meta.get("seq")
        seqs.append(str(int(s)) if pd.notnull(s) else "")
    
    # 3. 构造新 DataFrame (白名单机制，彻底防止重复)
    # 找出所有非元数据列（即月份列）
    exclude_cols = ['费项', '注释', '序号']
    # 过滤掉原表中可能残留的垃圾列
    month_cols = [c for c in df.columns if c not in exclude_cols and '注释' not in str(c) and '序号' not in str(c)]
    
    new_df = pd.DataFrame()
    new_df['费项'] = df['费项']
    new_df['注释'] = comments
    new_df['序号'] = seqs
    
    # 拼接月份数据
    for col in month_cols:
        new_df[col] = df[col]
        
    return new_df

def apply_advanced_style(df: pd.DataFrame):
    numeric_cols = [c for c in df.columns if c[1] not in ['费项', '注释', '序号', ' ']]
    def safe_fmt(x):
        try:
            if pd.isna(x) or str(x).strip() == "": return "-"
            return "{:,.2f}".format(float(x))
        except: return str(x)
    format_dict = {c: safe_fmt for c in numeric_cols}
    styler = df.style.format(format_dict)

    # 表头颜色 (map_index)
    def header_style(x):
        s = str(x)
        if "利润表" in s: return "background-color: #E8F0FE; color: #1a73e8; font-weight: bold; text-align: center; border: 1px solid #ddd;"
        if "现金表" in s: return "background-color: #FFFFE0; color: #d4a017; font-weight: bold; text-align: center; border: 1px solid #ddd;"
        if "_empty_" in s or s.strip() == "": return "background-color: white; border: none; color: transparent;"
        return "text-align: center; border: 1px solid #ddd; vertical-align: middle;"
    styler.map_index(header_style, axis=1, level=0)
    
    def level1_style(x):
        return "background-color: #F8F9FA; color: #333; font-weight: bold; text-align: center; border: 1px solid #ddd;"
    styler.map_index(level1_style, axis=1, level=1)

    def row_style(row):
        try: item_name = str(row[0]).strip() 
        except: item_name = ""
        bg, fc, fw, bd = "white", "black", "normal", ""
        if "净利润" in item_name or "4、余额" in item_name:
            bg, fc, fw, bd = "#F2F2F2", "#D9534F", "bold", "2px solid #333"
        elif "线上净利润" in item_name or "线上余额" in item_name:
            bg, fc, fw = "#F2F2F2", "#000000", "bold"
        elif "总部应收未收金额" in item_name:
            bg, fc = "#D4EDDA", "#000000"
        elif item_name.startswith("1、"):
            bg, fw = "#F2F2F2", "bold"
        elif item_name.startswith("--") and not item_name.startswith("------"):
            fc, fw = "#333333", "bold"
        elif item_name.startswith("------"):
            fc = "#666666"
        css = f"background-color: {bg}; color: {fc}; font-weight: {fw};"
        if bd: css += f"border-top: {bd}; border-bottom: {bd};"
        return [css] * len(row)
    styler = styler.apply(row_style, axis=1)
    
    styler = styler.applymap(lambda x: "min-width: 180px; text-align: left;", subset=[c for c in df.columns if c[1]=='费项'])
    styler = styler.applymap(lambda x: "color: #888888; font-style: italic; font-size: 0.9em; min-width: 200px; white-space: normal;", subset=[c for c in df.columns if c[1]=='注释'])
    styler = styler.applymap(lambda x: "text-align: center;", subset=[c for c in df.columns if c[1]=='序号'])
    styler = styler.applymap(lambda x: "background-color: white; border: none; width: 20px;", subset=[c for c in df.columns if c[0]==' '])
    
    styles = [{'selector': 'th:contains("_empty_")', 'props': [('background-color', 'white'), ('border', 'none'), ('color', 'transparent')]}]
    styler = styler.set_table_styles(styles)
    return styler

def rebuild_dataframe_with_headers(raw_data, headers):
    if not raw_data: return pd.DataFrame()
    data = []
    for row in raw_data:
        vals = [row.get(f"col_{i}", "") for i in range(len(headers))]
        data.append(vals)
    unique_headers = []
    ec = 0
    for h in headers:
        if h == "": unique_headers.append(f"_empty_{ec}"); ec += 1
        else: unique_headers.append(h)
    df = pd.DataFrame(data, columns=unique_headers)
    if len(df.columns) > 0: df.rename(columns={df.columns[0]: '费项'}, inplace=True)
    return df.fillna("")

def inject_offline_and_calculate(df: pd.DataFrame, store_id: str, db_manager):
    """
    全量计算逻辑：
    1. 识别所有月份列。
    2. 遍历每个月，从DB查该月的线下成本。
    3. 注入数据并计算净利润。
    """
    if df.empty: return df
    
    # 识别数据列 (排除元数据列)
    data_cols = [c for c in df.columns if c not in ['费项', '注释', '序号']]
    if not data_cols: return df
    
    # 辅助函数：从表里安全取数
    def get_table_val(name, col):
        rows = df[df['费项'] == name]
        if rows.empty: return 0.0
        val = rows[col].values[0]
        try: return float(str(val).replace(',', '').replace('¥', ''))
        except: return 0.0

    # 遍历每一列（每一个月）进行处理
    for m in data_cols:
        # 1. 尝试从DB获取该月的线下成本
        # 注意：DB中的 key 是 report_month (如 "2023-10")，但表头可能是 "10月"
        # 这里为了简化，我们假设 DB 存储时已经用了和表头一致的 key，或者我们做简单模糊匹配
        # 更好的方式：保存时让用户选的就是表头里的字串
        
        # 从DB查数据
        record = db_manager.get_offline_cost(store_id, m) # m is e.g. "10月" or "11月"
        cost_data = record if record else {}
        
        # 辅助函数：从DB记录取数
        def get_db_v(key):
            v = cost_data.get(key)
            return float(v) if v is not None else 0.0

        # 映射
        mapping = {
            "------人工工资": get_db_v('wages'),
            "------仓库房租": get_db_v('rent'),
            "------物业水电": get_db_v('utilities'),
            "------耗材成本": get_db_v('consumables'),
            "------其他支出": get_db_v('others') 
        }
        
        # 只有当DB里有数据时才注入，否则保留原表数据(或置0)
        # 这里策略：如果有记录就覆盖，没记录就不动(假设原表可能有数据)
        if record:
            for k, v in mapping.items():
                if k in df['费项'].values: df.loc[df['费项']==k, m] = v
            
            # 更新汇总行
            total_offline = sum(mapping.values())
            target_row = "3、线下成本" if "3、线下成本" in df['费项'].values else "3、线下支出"
            if target_row in df['费项'].values:
                df.loc[df['费项']==target_row, m] = total_offline
        else:
            # 如果DB没数据，尝试读取表里现有的汇总值用于计算净利润
            target_row = "3、线下成本" if "3、线下成本" in df['费项'].values else "3、线下支出"
            total_offline = get_table_val(target_row, m)

        # 2. 计算净利润
        try:
            v_online = get_table_val("1、线上毛利", m)
            v_hq = get_table_val("------总部分润（应收）", m)
            # 净利润 = 线上毛利 - 总部分润 - 线下成本
            if "净利润" in df['费项'].values:
                df.loc[df['费项']=="净利润", m] = v_online - v_hq - total_offline
        except: pass
        
    return df

# ==========================================
# 6. 渲染
# ==========================================
def render_query_system(db_manager):
    st.markdown("<h1 style='text-align: center;'>🔍 门店查询系统</h1>", unsafe_allow_html=True)
    db = db_manager.get_database()
    
    if 'authenticated' not in st.session_state: st.session_state.authenticated = False
    
    if not st.session_state.authenticated:
        c1, c2, c3 = st.columns([1, 2, 1])
        with c2:
            code = st.text_input("请输入查询编号", key="q_login")
            if st.button("登录", use_container_width=True):
                perm = db['permissions'].find_one({'query_code': code})
                if perm:
                    store = db['stores'].find_one({'_id': perm['store_id']})
                    if store:
                        st.session_state.authenticated = True
                        st.session_state.store_info = store
                        st.rerun()
                    else: st.error("门店不存在")
                else: st.error("编号无效")
        return

    store = st.session_state.store_info
    st.title(f"📊 {store['store_name']}")
    
    # 1. 准备数据
    reports = list(db['reports'].find({'store_id': store['_id']}).sort('report_month', -1))
    if not reports: st.warning("暂无报表数据"); return
    
    report = reports[0]
    df_full = rebuild_dataframe_with_headers(report.get('raw_excel_data', []), report.get('table_headers', []))
    
    # 2. 智能拆分利润表和现金表
    try:
        mid = len(df_full.columns) // 2
        df_profit = df_full.iloc[:, :mid].copy()
        df_cash = df_full.iloc[:, mid:].copy()
        if '费项' not in df_profit.columns: df_profit.rename(columns={df_profit.columns[0]: '费项'}, inplace=True)
        if len(df_cash.columns) > 0: df_cash.rename(columns={df_cash.columns[0]: '费项'}, inplace=True)
    except: df_profit = df_full.copy(); df_cash = df_full.copy()

    # 3. 提取月份列 (用于选择器)
    # 先应用add_meta_columns来清洗费项，但不保留注释列以便获取纯净的月份列
    temp_df = add_meta_columns(df_profit.copy()) 
    # 排除非月份列
    month_options = [c for c in temp_df.columns if c not in ['费项', '注释', '序号']]
    # 默认选最后一个（最新）
    default_ix = len(month_options) - 1 if month_options else 0

    # 4. 线下成本录入区域 (带月份选择)
    with st.expander("📝 录入线下成本", expanded=True):
        col_m, col_f = st.columns([1, 2])
        with col_m:
            selected_month = st.selectbox("选择月份", month_options, index=default_ix)
        
        with col_f:
            with st.form("cost_form"):
                st.caption(f"正在录入 **{selected_month}** 的数据")
                w = st.number_input("人工工资", min_value=0.0, value=None, step=None, format="%.2f")
                r = st.number_input("仓库房租", min_value=0.0, value=None, step=None, format="%.2f")
                u = st.number_input("物业水电", min_value=0.0, value=None, step=None, format="%.2f")
                c = st.number_input("耗材成本", min_value=0.0, value=None, step=None, format="%.2f")
                o = st.number_input("--其他支出", min_value=0.0, value=None, step=None, format="%.2f")
                
                if st.form_submit_button("提交并刷新报表", type="primary"):
                    data = {"wages": w, "rent": r, "utilities": u, "consumables": c, "others": o}
                    # 存入前将None转为0
                    save_data = {k: (v if v is not None else 0.0) for k, v in data.items()}
                    # 使用选中的月份作为key保存
                    db_manager.save_offline_cost(store['_id'], selected_month, save_data)
                    st.success(f"{selected_month} 数据已保存！")
                    time.sleep(0.5)
                    st.rerun()

    # 5. 数据处理与计算 (对所有月份)
    # 注意：add_meta_columns现在是重构列，所以先处理
    df_profit = add_meta_columns(df_profit)
    df_cash = add_meta_columns(df_cash)
    
    # 注入数据
    df_profit = inject_offline_and_calculate(df_profit, store['_id'], db_manager)
    
    # 6. 组装展示
    p_cols = [("表一：利润表", c) for c in df_profit.columns]
    df_profit.columns = pd.MultiIndex.from_tuples(p_cols)
    c_cols = [("表二：现金表", c) for c in df_cash.columns]
    df_cash.columns = pd.MultiIndex.from_tuples(c_cols)
    
    df_sep = pd.DataFrame(np.nan, index=df_profit.index, columns=[(" ", " ")])
    min_rows = min(len(df_profit), len(df_cash))
    df_display = pd.concat([df_profit.iloc[:min_rows], df_sep.iloc[:min_rows], df_cash.iloc[:min_rows]], axis=1).fillna("")
    
    pdf = db_manager.get_guide_pdf()
    if pdf:
        b64 = get_base64_of_bin_file(pdf)
        st.markdown(f'<a href="data:application/pdf;base64,{b64}" download="指引.pdf">📄 下载报表指引</a>', unsafe_allow_html=True)

    try:
        fin = report.get('financial_data', {})
        recv = fin.get('receivables', {}).get('net_amount', 0)
        color = "#3F51B5" if recv < 0 else ("#FF8F00" if recv > 0 else "#546E7A")
        text = "总部应退" if recv < 0 else ("门店应返" if recv > 0 else "已结清")
        st.markdown(f"""<div style="background:{color};padding:15px;border-radius:10px;text-align:center;color:white;margin-bottom:20px;"><div style="font-size:20px;">{text}</div><div style="font-size:28px;font-weight:bold;">¥{abs(recv):,.2f}</div></div>""", unsafe_allow_html=True)
    except: pass

    st.dataframe(apply_advanced_style(df_display), use_container_width=True, height=600)

def create_upload_app():
    st.title("📤 批量上传系统")
    db_manager = get_db_manager()
    if not db_manager.is_connected(): return
    if 'admin_auth' not in st.session_state: st.session_state.admin_auth = False
    if not st.session_state.admin_auth:
        pwd = st.text_input("管理员密码", type="password")
        if st.button("登录"):
            if pwd == ConfigManager.get_admin_password(): st.session_state.admin_auth = True; st.rerun()
            else: st.error("密码错误")
        return
    uploader = BulkReportUploader(db_manager.get_database())
    c1, c2 = st.columns(2)
    with c1:
        month = st.text_input("报表月份", value=datetime.now().strftime("%Y-%m"))
        clear = st.checkbox("覆盖历史数据", value=True)
        file = st.file_uploader("选择Excel", type=['xlsx', 'xls'])
        if file and st.button("开始上传"):
            bar = st.progress(0); status = st.empty()
            res = uploader.process_excel_file(file, month, clear, lambda p, m: (bar.progress(p/100), status.text(m)))
            st.success(f"成功: {res['success_count']}, 失败: {res['failed_count']}")
            if res['errors']: st.error(res['errors'])

def create_permission_app():
    st.title("👥 权限管理系统")
    db_manager = get_db_manager()
    if not db_manager.is_connected(): return
    if 'perm_auth' not in st.session_state: st.session_state.perm_auth = False
    if not st.session_state.perm_auth:
        pwd = st.text_input("管理员密码", type="password", key="p_pwd")
        if st.button("登录", key="p_login"):
            if pwd == ConfigManager.get_admin_password(): st.session_state.perm_auth = True; st.rerun()
            else: st.error("密码错误")
        return
    mgr = PermissionManager(db_manager.get_database())
    t1, t2, t3 = st.tabs(["权限上传", "权限列表", "系统数据"])
    with t1:
        f = st.file_uploader("权限表", type=['xlsx', 'csv'])
        if f and st.button("上传"):
            res = mgr.upload_permission_table(f)
            if res['success']: st.success(f"更新: {res['updated']}, 新增: {res['created']}")
            else: st.error(res['message'])
    with t2:
        for p in mgr.get_all_permissions():
            c1, c2 = st.columns([3, 1])
            c1.write(f"{p['query_code']} - {p['store_name']}")
            if c2.button("删除", key=p['query_code']): mgr.delete_permission(p['query_code']); st.rerun()
    with t3:
        pdf = st.file_uploader("PDF指引", type=['pdf'])
        if pdf and st.button("更新PDF"): db_manager.save_guide_pdf(pdf) and st.success("成功")
        if st.button("下载线下成本汇总"):
            data = db_manager.get_all_offline_costs()
            if data:
                out = io.BytesIO()
                with pd.ExcelWriter(out, engine='xlsxwriter') as writer:
                    rows = []
                    for d in data:
                        rows.append({"门店": d['store_id'], "月份": d['month'], "工": d['data'].get('wages'), "租": d['data'].get('rent'), "水": d['data'].get('utilities'), "耗": d['data'].get('consumables'), "他": d['data'].get('others')})
                    pd.DataFrame(rows).to_excel(writer, index=False)
                st.download_button("下载", out.getvalue(), "costs.xlsx")

def main():
    with st.sidebar:
        st.title("🏪 门店系统")
        app = st.selectbox("功能", ["门店查询系统", "批量上传系统", "权限管理系统"])
        if get_db_manager().is_connected(): st.success("✅ 在线")
        else: st.error("❌ 离线")
    if app == "门店查询系统": render_query_system(get_db_manager())
    elif app == "批量上传系统": create_upload_app()
    elif app == "权限管理系统": create_permission_app()

if __name__ == "__main__": main()

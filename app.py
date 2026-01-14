# streamlit_app.py - 门店报表系统 (格式化修复版)
"""
包含所有模块：
1. 门店查询：带双层表头、特定行变色、自动读取总部分润、线下成本录入(垂直无步进)。
2. 批量上传：解析Excel、存入MongoDB。
3. 权限管理：权限表导入、PDF管理、线下成本数据下载。
4. 修复：数据库连接判断、缺少的方法、数值格式化报错。
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
            if self.db is not None:
                if "offline_costs" not in self.db.list_collection_names():
                    self.db.create_collection("offline_costs")
                self._create_indexes()
        except Exception as e:
            st.error(f"连接失败: {e}")
            self.db = None
    
    def _create_indexes(self):
        if self.db is None: return
        try:
            self.db['stores'].create_index([("store_code", 1)], background=True)
            self.db['permissions'].create_index([("query_code", 1)], background=True)
            self.db['reports'].create_index([("store_id", 1), ("report_month", -1)], background=True)
            self.db['offline_costs'].create_index([("store_id", 1), ("month", 1)], background=True)
        except Exception: pass

    def get_database(self):
        return self.db

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
# 3. 数据模型 (Models)
# ==========================================
class StoreModel:
    @staticmethod
    def create_store_document(store_name: str, store_code: str = None, **kwargs) -> Dict:
        timestamp = int(datetime.now().timestamp())
        return {
            '_id': kwargs.get('_id', f"store_{store_code or store_name.replace(' ', '_')}_{timestamp}"),
            'store_name': store_name.strip(),
            'store_code': store_code or StoreModel._generate_store_code(store_name),
            'region': kwargs.get('region', '未分类'),
            'created_at': kwargs.get('created_at', datetime.now()),
            'created_by': kwargs.get('created_by', 'system'),
            'status': kwargs.get('status', 'active'),
            'aliases': kwargs.get('aliases', [store_name.strip()])
        }
    
    @staticmethod
    def _generate_store_code(store_name: str) -> str:
        try:
            normalized = store_name.replace('犀牛百货', '').replace('门店', '').replace('店', '').strip()
            hash_obj = hashlib.md5(normalized.encode('utf-8'))
            return f"AUTO_{hash_obj.hexdigest()[:6].upper()}"
        except:
            return f"AUTO_{int(datetime.now().timestamp()) % 100000}"

class ReportModel:
    @staticmethod
    def create_report_document(store_data: Dict, report_month: str, excel_data: List[Dict], headers: List[str], **kwargs) -> Dict:
        return {
            'store_id': store_data['_id'],
            'store_code': store_data['store_code'],
            'store_name': store_data['store_name'],
            'report_month': report_month,
            'sheet_name': kwargs.get('sheet_name', store_data['store_name']),
            'raw_excel_data': excel_data,
            'table_headers': headers,
            'financial_data': kwargs.get('financial_data', {}),
            'created_at': kwargs.get('created_at', datetime.now()),
            'updated_at': datetime.now(),
            'uploaded_by': kwargs.get('uploaded_by', 'system')
        }
    
    @staticmethod
    def dataframe_to_dict_list(df: pd.DataFrame) -> tuple[List[Dict], List[str]]:
        headers = []
        for col in df.columns:
            col_str = str(col)
            if col_str.startswith('Unnamed:') or 'unnamed' in col_str.lower():
                headers.append("")
            else:
                headers.append(col_str)
        
        unique_headers = []
        empty_count = 0
        for header in headers:
            if header == "":
                unique_headers.append(f"_empty_{empty_count}")
                empty_count += 1
            else:
                unique_headers.append(header)
        
        df.columns = unique_headers
        result = []
        for index, row in df.iterrows():
            row_dict = {}
            for col_idx, value in enumerate(row):
                col_key = f"col_{col_idx}"
                if pd.isna(value):
                    row_dict[col_key] = ""
                elif isinstance(value, (int, float)):
                    row_dict[col_key] = float(value) if not pd.isna(value) else 0.0
                else:
                    value_str = str(value).strip()
                    if value_str.startswith('='):
                        if '平台内支出' in value_str: row_dict[col_key] = "--平台内支出"
                        else: row_dict[col_key] = value_str[1:]
                    else:
                        row_dict[col_key] = value_str
            result.append(row_dict)
        return result, headers

class PermissionModel:
    @staticmethod
    def create_permission_document(query_code: str, store_data: Dict, **kwargs) -> Dict:
        return {
            'query_code': query_code.strip(),
            'store_id': store_data['_id'],
            'store_name': store_data['store_name'],
            'store_code': store_data['store_code'],
            'created_at': kwargs.get('created_at', datetime.now()),
            'updated_at': datetime.now(),
            'created_by': kwargs.get('created_by', 'system'),
            'status': kwargs.get('status', 'active')
        }

# ==========================================
# 4. 业务逻辑类 (Uploader & Manager)
# ==========================================
class BulkReportUploader:
    def __init__(self, db):
        self.db = db
        self.stores_collection = self.db['stores']
        self.reports_collection = self.db['reports']
    
    def find_or_create_store(self, sheet_name: str) -> Optional[Dict]:
        normalized = sheet_name.replace('犀牛百货', '').replace('门店', '').replace('店', '').strip()
        try:
            store = self.stores_collection.find_one({
                '$or': [
                    {"store_name": sheet_name},
                    {"store_name": {"$regex": normalized, "$options": "i"}},
                    {"aliases": {"$in": [sheet_name, normalized]}}
                ]
            })
            if store: return store
        except: pass
        
        try:
            store_data = StoreModel.create_store_document(
                store_name=sheet_name.strip(),
                aliases=[sheet_name.strip(), normalized],
                created_by='bulk_upload'
            )
            self.stores_collection.insert_one(store_data)
            return store_data
        except: return None

    def process_excel_file(self, file_buffer, report_month: str, clear_history: bool = True, progress_callback=None) -> Dict:
        start_time = time.time()
        result = {'success_count': 0, 'failed_count': 0, 'errors': [], 'processed_stores': [], 'failed_stores': [], 'total_time': 0}
        
        try:
            if progress_callback: progress_callback(10, "读取Excel文件...")
            excel_data_display = pd.read_excel(file_buffer, sheet_name=None, engine='openpyxl', header=1)
            excel_data_financial = pd.read_excel(file_buffer, sheet_name=None, engine='openpyxl', header=3)
            
            if clear_history:
                self.reports_collection.delete_many({'report_month': report_month})
            
            total = len(excel_data_display)
            processed = 0
            
            for sheet_name in excel_data_display.keys():
                processed += 1
                if progress_callback: progress_callback(20 + int(processed/total*70), f"处理: {sheet_name}")
                
                try:
                    store = self.find_or_create_store(sheet_name)
                    if not store:
                        result['failed_stores'].append({'store_name': sheet_name, 'reason': '创建门店失败'})
                        result['failed_count'] += 1
                        continue
                    
                    df_display = excel_data_display[sheet_name].dropna(axis=1, how='all')
                    df_fin = excel_data_financial[sheet_name].dropna(axis=1, how='all')
                    
                    if df_display.empty: continue
                    
                    excel_data_dict, headers = ReportModel.dataframe_to_dict_list(df_display)
                    financial_data = self._extract_financial_data(df_fin)
                    
                    report = ReportModel.create_report_document(store, report_month, excel_data_dict, headers, sheet_name=sheet_name, financial_data=financial_data)
                    self.reports_collection.insert_one(report)
                    
                    result['success_count'] += 1
                    result['processed_stores'].append({'sheet_name': sheet_name, 'store_name': store['store_name']})
                    
                except Exception as e:
                    result['failed_count'] += 1
                    result['errors'].append(f"{sheet_name}: {e}")
                    
        except Exception as e:
            result['errors'].append(str(e))
            
        result['total_time'] = time.time() - start_time
        return result

    def _extract_financial_data(self, df: pd.DataFrame) -> Dict:
        fin_data = {'receivables': {}, 'profit': {}}
        try:
            total_cols = [i for i, c in enumerate(df.columns) if '合计' in str(c) or 'Total' in str(c) or 'sum' in str(c).lower()]
            if not total_cols: 
                num_counts = [(i, df.iloc[:, i].apply(lambda x: pd.to_numeric(x, errors='coerce')).notna().sum()) for i in range(len(df.columns))]
                num_counts.sort(key=lambda x:x[1], reverse=True)
                if len(num_counts) >= 2: total_cols = [num_counts[0][0], num_counts[1][0]]
            
            if len(df) >= 37 and len(total_cols) >= 2:
                val = df.iloc[36, total_cols[1]] 
                parsed = pd.to_numeric(val, errors='coerce')
                if not pd.isna(parsed):
                    fin_data['receivables']['net_amount'] = float(parsed)
        except: pass
        return fin_data

class PermissionManager:
    def __init__(self, db):
        self.db = db
        self.permissions = self.db['permissions']
        self.stores = self.db['stores']
        
    def upload_permission_table(self, file_obj) -> Dict:
        try:
            df = pd.read_csv(file_obj) if file_obj.name.endswith('.csv') else pd.read_excel(file_obj)
            q_col, s_col = None, None
            for c in df.columns:
                if any(x in str(c).lower() for x in ['查询', 'query', 'code']): q_col = c
                if any(x in str(c).lower() for x in ['门店', 'store', 'name']): s_col = c
            
            if not q_col or not s_col: 
                if len(df.columns) >= 2: q_col, s_col = df.columns[0], df.columns[1]
                else: return {"success": False, "message": "无法识别列"}
            
            res = {"success": True, "created": 0, "updated": 0}
            for _, row in df.iterrows():
                q_code = str(row[q_col]).strip()
                s_name = str(row[s_col]).strip()
                if not q_code or not s_name: continue
                
                store = self.stores.find_one({"store_name": s_name})
                if not store:
                    store = self.stores.find_one({"aliases": s_name})
                    if not store:
                        store = StoreModel.create_store_document(s_name, created_by='perm_upload')
                        self.stores.insert_one(store)
                
                perm = PermissionModel.create_permission_document(q_code, store)
                if self.permissions.find_one({"query_code": q_code}):
                    self.permissions.replace_one({"query_code": q_code}, perm)
                    res["updated"] += 1
                else:
                    self.permissions.insert_one(perm)
                    res["created"] += 1
            return res
        except Exception as e: return {"success": False, "message": str(e)}

    def get_all_permissions(self):
        return list(self.permissions.find().sort("query_code", 1))
    
    def delete_permission(self, code):
        self.permissions.delete_one({"query_code": code})

# ==========================================
# 5. 辅助函数 (样式与计算)
# ==========================================
def get_base64_of_bin_file(bin_file):
    data = bin_file.read()
    return base64.b64encode(data).decode()

def add_meta_columns(df: pd.DataFrame) -> pd.DataFrame:
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
    # 识别数值列
    numeric_cols = [c for c in df.columns if c[1] not in ['费项', '注释', '序号', ' ']]
    
    # 1. 定义安全格式化函数，避免ValueError
    def safe_fmt(x):
        try:
            if pd.isna(x) or str(x).strip() == "": return "-"
            return "{:,.2f}".format(float(x))
        except:
            return str(x)

    format_dict = {c: safe_fmt for c in numeric_cols}
    
    # 2. 序号列格式
    seq_cols = [c for c in df.columns if c[1] == '序号']
    for c in seq_cols:
        def seq_fmt(x):
            try: return f"{int(float(x))}"
            except: return ""
        format_dict[c] = seq_fmt
        
    styler = df.style.format(format_dict)

    def row_style(row):
        try: item_name = str(row[0]).strip() 
        except: item_name = ""
        bg, fc, fw, bd = "white", "black", "normal", ""
        
        # 1. 净利润, 4、余额: 灰底红字加粗
        if "净利润" in item_name or "4、余额" in item_name:
            bg, fc, fw, bd = "#F2F2F2", "#D9534F", "bold", "2px solid #333"
        # 2. 线上净利润, 线上余额: 灰底黑字加粗
        elif "线上净利润" in item_name or "线上余额" in item_name:
            bg, fc, fw = "#F2F2F2", "#000000", "bold"
        # 3. 总部应收未收: 绿底黑字
        elif "总部应收未收金额" in item_name:
            bg, fc = "#D4EDDA", "#000000"
        # 4. 其他
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

    styles = [
        {'selector': 'th', 'props': [('text-align', 'center'), ('border', '1px solid #ddd'), ('vertical-align', 'middle')]},
        {'selector': 'th:contains("利润表")', 'props': [('background-color', '#E8F0FE !important'), ('color', '#1a73e8')]},
        {'selector': 'th:contains("现金表")', 'props': [('background-color', '#FFFFE0 !important'), ('color', '#d4a017')]},
        {'selector': 'th:contains("_empty_")', 'props': [('background-color', 'white'), ('border', 'none'), ('color', 'transparent')]},
    ]
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
        if h == "": 
            unique_headers.append(f"_empty_{ec}")
            ec += 1
        else: unique_headers.append(h)
        
    df = pd.DataFrame(data, columns=unique_headers)
    if len(df.columns) > 0: df.rename(columns={df.columns[0]: '费项'}, inplace=True)
    return df.fillna("")

def inject_offline_and_calculate(df: pd.DataFrame, offline_data: dict):
    if df.empty: return df
    data_cols = [c for c in df.columns if c not in ['费项', '注释', '序号']]
    if not data_cols: return df
    current_month = data_cols[-1]
    
    mapping = {
        "------人工工资支出": offline_data.get('wages', 0),
        "------仓库房租支出": offline_data.get('rent', 0),
        "------物业水电支出": offline_data.get('utilities', 0),
        "------耗材成本支出": offline_data.get('consumables', 0),
        "------其他费用": offline_data.get('others', 0) 
    }
    
    for k, v in mapping.items():
        if k in df['费项'].values: df.loc[df['费项']==k, current_month] = v
    
    total_offline = sum(mapping.values())
    if "3、线下成本" in df['费项'].values: df.loc[df['费项']=="3、线下成本", current_month] = total_offline
    
    try:
        def get_val(name, col):
            rows = df[df['费项'] == name]
            if rows.empty: return 0.0
            val = rows[col].values[0]
            try: return float(str(val).replace(',', '').replace('¥', ''))
            except: return 0.0

        for m in data_cols:
            v_online = get_val("1、线上毛利", m)
            v_hq = get_val("------总部分润（应收）", m)
            v_off = total_offline if m == current_month else get_val("3、线下成本", m)
            if "净利润" in df['费项'].values:
                df.loc[df['费项']=="净利润", m] = v_online - v_hq - v_off
    except: pass
    return df

# ==========================================
# 6. 各应用模块
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
                        st.session_state.cost_submitted = False
                        st.rerun()
                    else: st.error("门店不存在")
                else: st.error("编号无效")
        return

    # 已登录
    store = st.session_state.store_info
    st.title(f"📊 {store['store_name']}")
    
    # 线下成本 (垂直表单，无step)
    if not st.session_state.get('cost_submitted', False):
        st.info("请录入本期线下成本（直接输入金额，无需加减号）：")
        with st.form("cost_form"):
            w = st.number_input("人工工资支出", min_value=0.0, format="%.2f")
            r = st.number_input("仓库房租支出", min_value=0.0, format="%.2f")
            u = st.number_input("物业水电支出", min_value=0.0, format="%.2f")
            c = st.number_input("耗材成本支出", min_value=0.0, format="%.2f")
            o = st.number_input("--其他费用", min_value=0.0, format="%.2f", help="输入金额将直接增加线下成本总额")
            
            if st.form_submit_button("提交并生成报表", type="primary"):
                data = {"wages": w, "rent": r, "utilities": u, "consumables": c, "others": o}
                st.session_state.offline_data = data
                st.session_state.cost_submitted = True
                
                reports = list(db['reports'].find({'store_id': store['_id']}).sort('report_month', -1))
                latest_month = reports[0]['report_month'] if reports else datetime.now().strftime("%Y-%m")
                db_manager.save_offline_cost(store['_id'], latest_month, data)
                st.rerun()
        return

    # 报表展示
    reports = list(db['reports'].find({'store_id': store['_id']}).sort('report_month', -1))
    if not reports:
        st.warning("暂无报表数据")
        return

    report = reports[0]
    raw_data = report.get('raw_excel_data', [])
    headers = report.get('table_headers', [])
    df_full = rebuild_dataframe_with_headers(raw_data, headers)
    
    # 智能拆分
    try:
        mid = len(df_full.columns) // 2
        df_profit = df_full.iloc[:, :mid].copy()
        df_cash = df_full.iloc[:, mid:].copy()
        if '费项' not in df_profit.columns: df_profit.rename(columns={df_profit.columns[0]: '费项'}, inplace=True)
        if len(df_cash.columns) > 0: df_cash.rename(columns={df_cash.columns[0]: '费项'}, inplace=True)
    except:
        df_profit = df_full.copy(); df_cash = df_full.copy()

    df_profit = inject_offline_and_calculate(df_profit, st.session_state.offline_data)
    df_profit = add_meta_columns(df_profit)
    df_cash = add_meta_columns(df_cash)
    
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

    # 看板
    try:
        fin = report.get('financial_data', {})
        recv = fin.get('receivables', {}).get('net_amount', 0)
        color = "#3F51B5" if recv < 0 else ("#FF8F00" if recv > 0 else "#546E7A")
        text = "总部应退" if recv < 0 else ("门店应返" if recv > 0 else "已结清")
        st.markdown(f"""
        <div style="background:{color};padding:15px;border-radius:10px;text-align:center;color:white;margin-bottom:20px;">
            <div style="font-size:20px;">{text}</div>
            <div style="font-size:28px;font-weight:bold;">¥{abs(recv):,.2f}</div>
        </div>
        """, unsafe_allow_html=True)
    except: pass

    st.dataframe(apply_advanced_style(df_display), use_container_width=True, height=600)
    
    if st.button("修改线下成本"):
        st.session_state.cost_submitted = False
        st.rerun()

def create_upload_app():
    st.title("📤 批量上传系统")
    db_manager = get_db_manager()
    if not db_manager.is_connected(): return
    
    if 'admin_auth' not in st.session_state: st.session_state.admin_auth = False
    
    if not st.session_state.admin_auth:
        pwd = st.text_input("管理员密码", type="password")
        if st.button("登录"):
            if pwd == ConfigManager.get_admin_password():
                st.session_state.admin_auth = True
                st.rerun()
            else: st.error("密码错误")
        return

    uploader = BulkReportUploader(db_manager.get_database())
    c1, c2 = st.columns(2)
    with c1:
        month = st.text_input("报表月份", value=datetime.now().strftime("%Y-%m"))
        clear = st.checkbox("覆盖历史数据", value=True)
        file = st.file_uploader("选择Excel", type=['xlsx', 'xls'])
        if file and st.button("开始上传"):
            bar = st.progress(0)
            status = st.empty()
            def update(p, m): bar.progress(p/100); status.text(m)
            res = uploader.process_excel_file(file, month, clear, update)
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
            if pwd == ConfigManager.get_admin_password():
                st.session_state.perm_auth = True
                st.rerun()
            else: st.error("密码错误")
        return

    mgr = PermissionManager(db_manager.get_database())
    t1, t2, t3 = st.tabs(["权限上传", "权限列表", "系统数据"])
    
    with t1:
        f = st.file_uploader("权限表(Excel/CSV)", type=['xlsx', 'csv'])
        if f and st.button("上传"):
            res = mgr.upload_permission_table(f)
            if res['success']: st.success(f"新增: {res['created']}, 更新: {res['updated']}")
            else: st.error(res['message'])
    with t2:
        perms = mgr.get_all_permissions()
        if perms:
            for p in perms:
                c1, c2 = st.columns([3, 1])
                c1.write(f"{p['query_code']} - {p['store_name']}")
                if c2.button("删除", key=p['query_code']):
                    mgr.delete_permission(p['query_code'])
                    st.rerun()
    with t3:
        st.subheader("PDF指引")
        pdf = st.file_uploader("上传PDF", type=['pdf'])
        if pdf and st.button("更新PDF"):
            if db_manager.save_guide_pdf(pdf): st.success("成功")
        st.subheader("线下成本导出")
        if st.button("下载汇总表"):
            data = db_manager.get_all_offline_costs()
            if data:
                out = io.BytesIO()
                with pd.ExcelWriter(out, engine='xlsxwriter') as writer:
                    # 扁平化数据
                    rows = []
                    for d in data:
                        r = {
                            "门店ID": d['store_id'], 
                            "月份": d['month'],
                            "提交时间": d.get('updated_at', ''),
                            "工资": d['data'].get('wages', 0),
                            "房租": d['data'].get('rent', 0),
                            "水电": d['data'].get('utilities', 0),
                            "耗材": d['data'].get('consumables', 0),
                            "其他": d['data'].get('others', 0)
                        }
                        rows.append(r)
                    
                    # 按门店分sheet
                    df_all = pd.DataFrame(rows)
                    for sid in df_all['门店ID'].unique():
                        sub_df = df_all[df_all['门店ID'] == sid]
                        safe_name = str(sid)[:30].replace(':','').replace('/','')
                        sub_df.to_excel(writer, sheet_name=safe_name, index=False)
                        
                st.download_button("📥 下载Excel", out.getvalue(), "costs.xlsx")
            else: st.warning("无数据")

def main():
    with st.sidebar:
        st.title("🏪 门店系统")
        app = st.selectbox("功能", ["门店查询系统", "批量上传系统", "权限管理系统"])
        if get_db_manager().is_connected(): st.success("✅ 在线")
        else: st.error("❌ 离线")
        
    if app == "门店查询系统": render_query_system(get_db_manager())
    elif app == "批量上传系统": create_upload_app()
    elif app == "权限管理系统": create_permission_app()

if __name__ == "__main__":
    main()

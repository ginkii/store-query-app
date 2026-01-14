# streamlit_app.py - 门店报表系统完整版 (融合新需求)
"""
门店报表查询系统 - 完整功能单文件部署版本
包含查询、上传、权限管理、线下成本录入、样式美化及PDF管理功能
"""

import streamlit as st
import pandas as pd
import numpy as np
import pymongo
from pymongo import MongoClient
import gridfs  # 新增: 用于存储PDF
import plotly.express as px
import plotly.graph_objects as go
import traceback
import os
import time
import hashlib
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import io
import base64
import xlsxwriter  # 新增: 用于后台导出Excel

# ==========================================
# 0. 常量定义 (新增)
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

# 配置管理
class ConfigManager:
    """配置管理器"""
    
    @staticmethod
    def get_mongodb_config():
        """获取MongoDB配置"""
        try:
            if hasattr(st, 'secrets') and 'mongodb' in st.secrets:
                return {
                    'uri': st.secrets["mongodb"]["uri"],
                    'database_name': st.secrets["mongodb"]["database_name"]
                }
        except Exception:
            pass
        
        return {
            'uri': os.getenv('MONGODB_URI', 'mongodb://localhost:27017/'),
            'database_name': os.getenv('DATABASE_NAME', 'store_reports')
        }
    
    @staticmethod
    def get_admin_password():
        """获取管理员密码"""
        try:
            if hasattr(st, 'secrets') and 'security' in st.secrets:
                return st.secrets["security"]["admin_password"]
        except Exception:
            pass
        return os.getenv('ADMIN_PASSWORD', 'admin123')

# 数据库管理
try:
    import pymongo
    from pymongo import MongoClient
    PYMONGO_AVAILABLE = True
except ImportError:
    PYMONGO_AVAILABLE = False

class DatabaseManager:
    """数据库管理器"""
    
    def __init__(self):
        self.db = None
        self.client = None
        self.fs = None  # GridFS
        self._connect()
    
    def _connect(self):
        """建立数据库连接"""
        if not PYMONGO_AVAILABLE:
            st.error("PyMongo未安装，请检查requirements.txt文件")
            return
            
        try:
            config = ConfigManager.get_mongodb_config()
            self.client = MongoClient(config['uri'], serverSelectionTimeoutMS=5000)
            self.db = self.client[config['database_name']]
            self.fs = gridfs.GridFS(self.db)  # 初始化GridFS
            
            # 测试连接
            self.db.command('ping')
            self._create_indexes()
            
            # 确保线下成本集合存在
            if "offline_costs" not in self.db.list_collection_names():
                self.db.create_collection("offline_costs")
            
        except Exception as e:
            error_msg = f"数据库连接失败: {e}"
            if "ServerSelectionTimeoutError" in str(type(e)):
                error_msg += "\n💡 提示：请检查MongoDB URI和网络连接"
            elif "Authentication" in str(e):
                error_msg += "\n💡 提示：请检查数据库用户名和密码"
            
            st.error(error_msg)
            self.db = None
            self.client = None
    
    # ... (前面的代码保持不变)

    def _create_indexes(self):
        """创建索引"""
        # 修改点 1: 改为 is None
        if self.db is None: return
        try:
            self.db['stores'].create_index([("store_code", 1)], background=True)
            self.db['permissions'].create_index([("query_code", 1)], background=True)
            self.db['reports'].create_index([("store_id", 1), ("report_month", -1)], background=True)
            self.db['offline_costs'].create_index([("store_id", 1), ("month", 1)], background=True)
        except Exception:
            pass
    
    def get_database(self):
        return self.db
    
    def is_connected(self):
        # 修改点 2: 改为 is not None
        return self.db is not None

    # --- 新增功能方法 ---

    def save_guide_pdf(self, file_obj):
        """保存财务指引PDF"""
        # gridfs 对象同理，建议也改为显式判断
        if self.fs is None: return False
        try:
            old = self.fs.find_one({"filename": "guide.pdf"})
            if old: self.fs.delete(old._id)
            self.fs.put(file_obj, filename="guide.pdf")
            return True
        except Exception: return False

    def get_guide_pdf(self):
        """获取财务指引PDF"""
        if self.fs is None: return None
        try: return self.fs.find_one({"filename": "guide.pdf"})
        except: return None

    def save_offline_cost(self, store_id, month, data):
        """保存线下成本"""
        # 修改点 3: 这里的报错就是因为这行代码
        # 原代码: if not self.db: return False
        # 修改后:
        if self.db is None: return False
        try:
            self.db["offline_costs"].update_one(
                {"store_id": store_id, "month": month},
                {"$set": {
                    "store_id": store_id, 
                    "month": month, 
                    "data": data, 
                    "updated_at": datetime.now()
                }},
                upsert=True
            )
            return True
        except Exception as e:
            st.error(f"保存失败: {e}")
            return False

    def get_offline_cost(self, store_id, month):
        """获取单条线下成本"""
        # 修改点 4: 改为 is None
        if self.db is None: return {}
        try:
            res = self.db["offline_costs"].find_one({"store_id": store_id, "month": month})
            return res.get("data", {}) if res else {}
        except: return {}

    def get_all_offline_costs(self):
        """获取所有线下成本(后台下载用)"""
        # 修改点 5: 改为 is None
        if self.db is None: return []
        try:
            return list(self.db["offline_costs"].find({}, {"_id": 0}).sort("store_id", 1))
        except: return []

# 全局数据库管理器
@st.cache_resource
def get_db_manager():
    return DatabaseManager()

# ==========================================
# 辅助函数: 样式与数据处理 (新增)
# ==========================================

def get_base64_of_bin_file(bin_file):
    """文件转base64"""
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
    
    # 插入列
    df.insert(1, '注释', comments)
    df.insert(2, '序号', seqs)
    return df

def inject_offline_and_calculate(df: pd.DataFrame, offline_data: dict):
    """注入数据并计算净利润"""
    # 找到月份列 (排除非数据列)
    non_data_cols = ['费项', '注释', '序号', 'col_0'] # col_0通常是原始的Item列，根据rebuild逻辑
    # 实际上rebuild后的columns是unique_headers。我们需要识别哪些是月份。
    # 简单策略：取最后一列作为最新月份
    
    if df.empty: return df
    
    # 假设最后一列是最新月份
    current_month_col = df.columns[-1]
    
    # 1. 注入细项
    mapping = {
        "------人工工资支出": offline_data.get('wages', 0),
        "------仓库房租支出": offline_data.get('rent', 0),
        "------物业水电支出": offline_data.get('utilities', 0),
        "------耗材成本支出": offline_data.get('consumables', 0),
        "------其他支出": offline_data.get('others', 0)
    }
    
    for k, v in mapping.items():
        if k in df['费项'].values:
            df.loc[df['费项']==k, current_month_col] = v
            
    # 2. 更新汇总 "3、线下支出"
    total_offline = sum(mapping.values())
    if "3、线下支出" in df['费项'].values:
        df.loc[df['费项']=="3、线下支出", current_month_col] = total_offline
        
    # 3. 计算净利润 (全量计算所有列，但只注入了最新月的线下成本)
    # 公式：净利润 = 1、线上毛利 - 2、经营费用 - 3、线下支出
    try:
        def get_val(name, col):
            rows = df[df['费项'] == name]
            if rows.empty: return 0.0
            val = rows[col].values[0]
            # 尝试转数字
            try:
                if isinstance(val, str):
                    val = float(val.replace(',', '').replace('¥', ''))
                return float(val)
            except:
                return 0.0

        # 遍历所有数据列（排除前几列元数据）
        # 假设前3列是 费项, 注释, 序号
        data_cols = df.columns[3:] 
        
        for m in data_cols:
            v1 = get_val("1、线上毛利", m)
            v2 = get_val("2、经营费用", m)
            
            # 线下支出：如果是最新月，用刚才算的；否则读表里的
            if m == current_month_col:
                v3 = total_offline
            else:
                v3 = get_val("3、线下支出", m)
                
            net = v1 - v2 - v3
            
            # 写入或更新净利润行
            if "净利润" in df['费项'].values:
                df.loc[df['费项']=="净利润", m] = net
            else:
                # 如果没有净利润行，需要追加。这在DataFrame中间追加比较麻烦，
                # 这里简化处理：如果有就更，没有就不更（通常模板会有）
                pass
                
    except Exception as e:
        print(f"计算出错: {e}")
        
    return df

def style_dataframe(df: pd.DataFrame, table_type: str = "profit"):
    """精细化样式控制"""
    # 识别数值列
    numeric_cols = [c for c in df.columns if c not in ['费项', '注释', '序号']]
    
    styler = df.style.format({c: "{:,.2f}" for c in numeric_cols}, na_rep="-")
    if '序号' in df.columns: styler = styler.format({'序号': "{:.0f}"})

    def row_style(row):
        name = str(row['费项']).strip()
        bg, fc, fw, fs, bd = "white", "black", "normal", "normal", ""
        
        if "净利润" in name:
            bg, fc, fw, bd = "#D4EDDA", "#D9534F", "bold", "2px solid #333"
        elif name.startswith("1、"):
            bg, fc, fw = "#F2F2F2", "#000000", "bold"
        elif name.startswith("--") and not name.startswith("------"):
            fc, fw, fs = "#333333", "bold", "italic"
        elif name.startswith("------"):
            fc = "#666666"
        elif re.match(r'^\d+、', name):
            bg, fw = "#F2F2F2", "bold"
            
        css = f"background-color: {bg}; color: {fc}; font-weight: {fw}; font-style: {fs};"
        if bd: css += f"border-top: {bd}; border-bottom: {bd};"
        return [css] * len(row)

    styler = styler.apply(row_style, axis=1)
    
    # 列样式
    styler = styler.applymap(lambda x: "min-width: 200px; text-align: left;", subset=['费项'])
    if '注释' in df.columns:
        styler = styler.applymap(lambda x: "color: #888888; font-style: italic; font-size: 0.9em; min-width: 250px; white-space: normal; text-align: left;", subset=['注释'])
    if '序号' in df.columns:
        styler = styler.applymap(lambda x: "text-align: center; width: 40px; color: #555;", subset=['序号'])

    # 表头
    header_bg = "#E8F0FE" if table_type == "profit" else "#E6FFFA"
    styler = styler.set_table_styles([
        {'selector': 'th', 'props': [('background-color', header_bg), ('color', 'black'), ('font-weight', 'bold'), ('text-align', 'center'), ('border', '1px solid #e0e0e0')]}
    ])
    return styler

# ==========================================
# 数据模型 (保持原样)
# ==========================================
class StoreModel:
    """门店数据模型"""
    @staticmethod
    def create_store_document(store_name: str, store_code: str = None, **kwargs) -> Dict:
        timestamp = int(datetime.now().timestamp())
        return {
            '_id': kwargs.get('_id', f"store_{store_code or store_name.replace(' ', '_')}_{timestamp}"),
            'store_name': store_name.strip(),
            'store_code': store_code or StoreModel._generate_store_code(store_name),
            'region': kwargs.get('region', '未分类'),
            'manager': kwargs.get('manager', '待设置'),
            'aliases': kwargs.get('aliases', [store_name.strip()]),
            'created_at': kwargs.get('created_at', datetime.now()),
            'created_by': kwargs.get('created_by', 'system'),
            'status': kwargs.get('status', 'active')
        }
    
    @staticmethod
    def _generate_store_code(store_name: str) -> str:
        try:
            normalized = store_name.replace('犀牛百货', '').replace('门店', '').replace('店', '').strip()
            hash_obj = hashlib.md5(normalized.encode('utf-8'))
            return f"AUTO_{hash_obj.hexdigest()[:6].upper()}"
        except Exception:
            return f"AUTO_{int(datetime.now().timestamp()) % 100000}"

class ReportModel:
    """报表数据模型"""
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
            if col_str.startswith('Unnamed:') or col_str.startswith('Unnamed ') or ('unnamed' in col_str.lower()):
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
                        if '平台内支出' in value_str:
                            row_dict[col_key] = "--平台内支出"
                        elif value_str.startswith('=--'):
                            row_dict[col_key] = value_str[3:]
                        else:
                            row_dict[col_key] = value_str[1:]
                    else:
                        row_dict[col_key] = value_str
            result.append(row_dict)
        
        return result, headers

class PermissionModel:
    """权限数据模型"""
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
# 业务逻辑类 (BulkReportUploader等)
# ==========================================
class BulkReportUploader:
    """批量报表上传器"""
    def __init__(self, db):
        if db is None: raise Exception("数据库连接失败")
        self.db = db
        self.stores_collection = self.db['stores']
        self.reports_collection = self.db['reports']
    
    def normalize_store_name(self, sheet_name: str) -> str:
        name = sheet_name.strip()
        name = name.replace('犀牛百货', '').replace('门店', '').replace('店', '')
        name = name.replace('(', '').replace(')', '').replace('（', '').replace('）', '')
        name = ''.join(name.split())
        return name
    
    def find_or_create_store(self, sheet_name: str) -> Optional[Dict]:
        normalized_name = self.normalize_store_name(sheet_name)
        search_patterns = [
            {"store_name": sheet_name},
            {"store_name": {"$regex": normalized_name, "$options": "i"}},
            {"aliases": {"$in": [sheet_name, normalized_name]}},
        ]
        for pattern in search_patterns:
            try:
                store = self.stores_collection.find_one(pattern)
                if store: return store
            except Exception: continue
        return self._create_store_from_sheet_name(sheet_name)
    
    def _create_store_from_sheet_name(self, sheet_name: str) -> Optional[Dict]:
        try:
            store_data = StoreModel.create_store_document(
                store_name=sheet_name.strip(),
                aliases=[sheet_name.strip(), self.normalize_store_name(sheet_name)],
                created_by='bulk_upload'
            )
            self.stores_collection.insert_one(store_data)
            return store_data
        except Exception as e:
            st.error(f"创建门店失败: {e}")
            return None
    
    def process_excel_file(self, file_buffer, report_month: str, clear_history: bool = True, progress_callback=None) -> Dict:
        start_time = time.time()
        result = {'success_count': 0, 'failed_count': 0, 'errors': [], 'processed_stores': [], 'failed_stores': [], 'total_time': 0, 'cleared_count': 0}
        
        try:
            if progress_callback: progress_callback(5, "准备上传，清理历史数据...")
            
            if clear_history:
                try:
                    clear_result = self.reports_collection.delete_many({'report_month': report_month})
                    result['cleared_count'] = clear_result.deleted_count
                    if progress_callback: progress_callback(10, f"已清除 {result['cleared_count']} 条历史数据")
                except Exception as e:
                    result['errors'].append(f"清除历史数据失败: {str(e)}")
            
            if progress_callback: progress_callback(15, "正在读取Excel文件...")
            
            excel_data_display = pd.read_excel(file_buffer, sheet_name=None, engine='openpyxl', header=1)
            excel_data_financial = pd.read_excel(file_buffer, sheet_name=None, engine='openpyxl', header=3)
            total_sheets = len(excel_data_display)
            
            if progress_callback: progress_callback(20, f"发现 {total_sheets} 个工作表，开始处理...")
            
            processed = 0
            for sheet_name in excel_data_display.keys():
                try:
                    processed += 1
                    progress = 20 + (processed / total_sheets) * 70
                    if progress_callback: progress_callback(progress, f"正在处理: {sheet_name}")
                    
                    store = self.find_or_create_store(sheet_name)
                    if not store:
                        result['failed_stores'].append({'store_name': sheet_name, 'reason': '无法创建门店记录'})
                        result['failed_count'] += 1
                        continue
                    
                    df_display = excel_data_display[sheet_name]
                    df_display_cleaned = df_display.dropna(axis=1, how='all')
                    
                    df_financial = excel_data_financial[sheet_name]
                    df_financial_cleaned = df_financial.dropna(axis=1, how='all')
                    
                    if df_display_cleaned.empty:
                        result['failed_stores'].append({'store_name': sheet_name, 'reason': '显示数据为空'})
                        result['failed_count'] += 1
                        continue
                    
                    excel_data_dict, headers = ReportModel.dataframe_to_dict_list(df_display_cleaned)
                    financial_data = self._extract_financial_data_v2(df_financial_cleaned)
                    
                    report_data = ReportModel.create_report_document(
                        store_data=store,
                        report_month=report_month,
                        excel_data=excel_data_dict,
                        headers=headers,
                        sheet_name=sheet_name,
                        financial_data=financial_data,
                        uploaded_by='bulk_upload'
                    )
                    
                    self.reports_collection.insert_one(report_data)
                    
                    result['success_count'] += 1
                    result['processed_stores'].append({'sheet_name': sheet_name, 'store_name': store['store_name'], 'store_code': store['store_code']})
                
                except Exception as e:
                    result['failed_stores'].append({'store_name': sheet_name, 'reason': f"处理错误: {str(e)}"})
                    result['failed_count'] += 1
                    result['errors'].append(f"{sheet_name}: {str(e)}")
            
            if progress_callback: progress_callback(100, "上传完成！")
            
        except Exception as e:
            result['errors'].append(f"文件处理失败: {str(e)}")
        
        result['total_time'] = time.time() - start_time
        return result
    
    def _extract_financial_data_v2(self, df: pd.DataFrame) -> Dict:
        financial_data = {'revenue': {}, 'cost': {}, 'profit': {}, 'receivables': {}, 'other_metrics': {}}
        try:
            total_col_indices = []
            for col_idx, col_name in enumerate(df.columns):
                col_str = str(col_name).lower().strip()
                if any(keyword in col_str for keyword in ['合计', 'total', '总计', '小计', 'sum', '汇总', '金额', '总金额']):
                    total_col_indices.append(col_idx)
            
            if not total_col_indices:
                numeric_counts = []
                for col_idx in range(len(df.columns)):
                    try:
                        numeric_count = df.iloc[:, col_idx].apply(lambda x: pd.to_numeric(x, errors='coerce')).notna().sum()
                        numeric_counts.append((col_idx, numeric_count))
                    except: numeric_counts.append((col_idx, 0))
                numeric_counts.sort(key=lambda x: x[1], reverse=True)
                if len(numeric_counts) >= 2: total_col_indices = [numeric_counts[0][0], numeric_counts[1][0]]
            
            if len(df) >= 37 and len(total_col_indices) >= 2:
                target_row_index = 36
                target_col_idx = total_col_indices[1]
                try:
                    raw_value = df.iloc[target_row_index, target_col_idx]
                    parsed_value = pd.to_numeric(raw_value, errors='coerce')
                    if not pd.isna(parsed_value):
                        financial_data['receivables']['net_amount'] = float(parsed_value)
                except: pass
            
            for idx, row in df.iterrows():
                try:
                    if len(row) < 2: continue
                    metric_name = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
                    if not metric_name: continue
                    
                    value = None
                    for col_idx in total_col_indices:
                        if col_idx < len(row):
                            try:
                                if pd.notna(row.iloc[col_idx]):
                                    value = float(row.iloc[col_idx])
                                    break
                            except: continue
                    if value is None:
                        for col_idx in range(1, len(row)):
                            if col_idx not in total_col_indices:
                                try:
                                    if pd.notna(row.iloc[col_idx]):
                                        value = float(row.iloc[col_idx])
                                        break
                                except: continue
                    if value is None: value = 0
                    
                    if '线上' in metric_name and '毛利' in metric_name: financial_data['profit']['gross_profit'] = value
                    if '净利' in metric_name: financial_data['profit']['net_profit'] = value
                except: continue
                
        except Exception as e: st.error(f"提取财务数据时出错: {e}")
        return financial_data

class PermissionManager:
    """权限管理器"""
    def __init__(self, db):
        if db is None: raise Exception("数据库连接失败")
        self.db = db
        self.permissions_collection = self.db['permissions']
        self.stores_collection = self.db['stores']
    
    def upload_permission_table(self, uploaded_file) -> Dict:
        try:
            if uploaded_file.name.endswith('.csv'): df = pd.read_csv(uploaded_file)
            else: df = pd.read_excel(uploaded_file)
            
            query_code_col, store_name_col = None, None
            for col in df.columns:
                if any(k in str(col).lower() for k in ['查询编号', 'query', 'code']): query_code_col = col; break
            for col in df.columns:
                if any(k in str(col).lower() for k in ['门店名称', 'store', 'name']): store_name_col = col; break
            
            if not query_code_col or not store_name_col:
                if len(df.columns) >= 2: query_code_col, store_name_col = df.columns[0], df.columns[1]
                else: return {"success": False, "message": "文件至少需要两列数据"}
            
            results = {"success": True, "processed": 0, "created": 0, "updated": 0, "errors": [], "detected_columns": {"query_code": str(query_code_col), "store_name": str(store_name_col)}}
            
            for _, row in df.iterrows():
                try:
                    query_code = str(row[query_code_col]).strip()
                    store_name = str(row[store_name_col]).strip()
                    if not query_code or not store_name or query_code == 'nan': continue
                    
                    store = self._find_or_create_store(store_name)
                    if not store:
                        results["errors"].append(f"无法处理门店: {store_name}")
                        continue
                    
                    existing = self.permissions_collection.find_one({'query_code': query_code})
                    perm_doc = PermissionModel.create_permission_document(query_code, store)
                    
                    if existing:
                        self.permissions_collection.replace_one({'query_code': query_code}, perm_doc)
                        results["updated"] += 1
                    else:
                        self.permissions_collection.insert_one(perm_doc)
                        results["created"] += 1
                    results["processed"] += 1
                except Exception as e: results["errors"].append(f"处理行出错: {e}")
            return results
        except Exception as e: return {"success": False, "message": f"处理文件出错: {e}"}
    
    def _find_or_create_store(self, store_name):
        try:
            store = self.stores_collection.find_one({'store_name': store_name})
            if store: return store
            clean_name = store_name.replace('犀牛百货', '').replace('门店', '').replace('店', '').strip()
            if clean_name:
                stores = list(self.stores_collection.find({'$or': [{'store_name': {'$regex': clean_name, '$options': 'i'}}, {'aliases': {'$in': [store_name, clean_name]}}]}))
                if stores: return stores[0]
            store_data = StoreModel.create_store_document(store_name=store_name, created_by='permission_upload')
            self.stores_collection.insert_one(store_data)
            return store_data
        except: return None
    
    def get_all_permissions(self):
        try: return list(self.permissions_collection.find().sort('query_code', 1))
        except: return []
    
    def delete_permission(self, query_code):
        try: return self.permissions_collection.delete_one({'query_code': query_code}).deleted_count > 0
        except: return False

def rebuild_dataframe_with_headers(raw_data: List[Dict], headers: List[str]) -> pd.DataFrame:
    if not raw_data or not headers: return pd.DataFrame()
    try:
        data_matrix = []
        for row_data in raw_data:
            row_values = []
            for col_idx in range(len(headers)):
                value = row_data.get(f"col_{col_idx}", "")
                row_values.append(value)
            data_matrix.append(row_values)
        
        unique_headers = []
        display_headers = []
        empty_count = 0
        for header in headers:
            display_headers.append(header)
            if header == "":
                unique_headers.append(f"_empty_{empty_count}")
                empty_count += 1
            else: unique_headers.append(header)
            
        df = pd.DataFrame(data_matrix, columns=unique_headers)
        df.attrs['display_headers'] = display_headers
        
        # 将 "费项" 列（通常是第一列）重命名为 "费项"，以便后续处理
        if not df.empty and len(df.columns) > 0:
            df.rename(columns={df.columns[0]: '费项'}, inplace=True)
            
        return df.fillna('')
    except Exception as e:
        st.error(f"重建表格失败: {e}")
        return pd.DataFrame()

# ==========================================
# 应用界面
# ==========================================
def create_query_app():
    """门店查询应用"""
    st.markdown("<h1 style='text-align: center;'>🔍 门店查询系统</h1>", unsafe_allow_html=True)
    
    db_manager = get_db_manager()
    if not db_manager.is_connected():
        st.error("数据库连接失败")
        return
    
    db = db_manager.get_database()
    
    if 'authenticated' not in st.session_state: st.session_state.authenticated = False
    
    if not st.session_state.authenticated:
        st.markdown("<h3 style='text-align: center;'>🔐 登录</h3>", unsafe_allow_html=True)
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            query_code = st.text_input("", placeholder="请输入查询编号")
            if st.button("登录", use_container_width=True):
                if query_code:
                    permission = db['permissions'].find_one({'query_code': query_code})
                    if permission:
                        store = db['stores'].find_one({'_id': permission['store_id']})
                        if store:
                            st.session_state.authenticated = True
                            st.session_state.store_info = store
                            st.session_state.query_code = query_code
                            # 重置线下成本状态
                            st.session_state.cost_submitted = False 
                            st.success(f"登录成功！欢迎 {store['store_name']}")
                            st.rerun()
                        else: st.error("门店信息不存在")
                    else: st.error("查询编号无效")
                else: st.warning("请输入查询编号")
    else:
        # 已登录
        store_info = st.session_state.store_info
        
        with st.sidebar:
            st.info(f"当前门店: {store_info['store_name']}")
            if st.button("退出登录"):
                st.session_state.authenticated = False
                st.rerun()
        
        st.title(f"📊 {store_info['store_name']}")
        
        # 获取最新报表
        reports = list(db['reports'].find({'store_id': store_info['_id']}).sort('report_month', -1))
        
        # --- 新增: 线下成本录入交互 ---
        if 'cost_submitted' not in st.session_state: st.session_state.cost_submitted = False
        
        # 如果还没提交过成本，且不是在查看历史状态 (简化处理：每次登录都要填)
        if not st.session_state.cost_submitted:
            st.info("请先录入本期线下成本，提交后生成报表。")
            with st.form("offline_cost_form"):
                st.markdown("### 💰 线下成本录入")
                c1, c2, c3 = st.columns(3)
                with c1:
                    wages = st.number_input("人工工资支出", min_value=0.0, step=100.0)
                    rent = st.number_input("仓库房租支出", min_value=0.0, step=100.0)
                with c2:
                    utilities = st.number_input("物业水电支出", min_value=0.0, step=100.0)
                    consumables = st.number_input("耗材成本支出", min_value=0.0, step=50.0)
                with c3:
                    others = st.number_input("其他支出", min_value=0.0, step=50.0)
                
                if st.form_submit_button("提交并生成报表", type="primary"):
                    cost_data = {
                        "wages": wages, "rent": rent, "utilities": utilities,
                        "consumables": consumables, "others": others
                    }
                    st.session_state.offline_data = cost_data
                    st.session_state.cost_submitted = True
                    
                    # 保存到DB
                    latest_month = reports[0]['report_month'] if reports else datetime.now().strftime("%Y-%m")
                    db_manager.save_offline_cost(store_info['_id'], latest_month, cost_data)
                    
                    st.rerun()
            return # 停止渲染后续内容，直到提交
            
        # --- 报表展示阶段 ---
        
        if reports:
            latest_report = reports[0]
            
            # 1. 顶部区域：PDF下载 + 看板
            col_pdf, col_kpi = st.columns([1, 4])
            with col_pdf:
                pdf_file = db_manager.get_guide_pdf()
                if pdf_file:
                    b64_pdf = get_base64_of_bin_file(pdf_file)
                    st.markdown(f'<a href="data:application/pdf;base64,{b64_pdf}" download="财务报表解读指引.pdf" style="display:inline-block;background:#FF4B4B;color:white;padding:10px 20px;border-radius:5px;text-decoration:none;font-weight:bold;margin-top:20px;">📄 下载报表指引</a>', unsafe_allow_html=True)
                else:
                    st.caption("暂无报表指引")

            with col_kpi:
                # 保留原有的看板逻辑 (总部应退/门店应返)
                receivables = latest_report.get('financial_data', {}).get('receivables', {})
                amount = receivables.get('net_amount', 0)
                
                if amount < 0:
                    st.markdown(f"""<div style="background:linear-gradient(135deg,#3F51B5,#7986CB);padding:20px;border-radius:10px;text-align:center;color:white;margin:10px;">
                        <div style="font-size:24px;">总部应退</div><div style="font-size:32px;font-weight:bold;">¥{abs(amount):,.2f}</div></div>""", unsafe_allow_html=True)
                elif amount > 0:
                    st.markdown(f"""<div style="background:linear-gradient(135deg,#FF8F00,#FFD54F);padding:20px;border-radius:10px;text-align:center;color:white;margin:10px;">
                        <div style="font-size:24px;">门店应返</div><div style="font-size:32px;font-weight:bold;">¥{amount:,.2f}</div></div>""", unsafe_allow_html=True)
                else:
                    st.markdown(f"""<div style="background:linear-gradient(135deg,#546E7A,#B0BEC5);padding:20px;border-radius:10px;text-align:center;color:white;margin:10px;">
                        <div style="font-size:24px;">已结清</div><div style="font-size:32px;font-weight:bold;">¥0.00</div></div>""", unsafe_allow_html=True)

            # 2. 报表展示 (Pandas Styler)
            st.subheader("详细报表")
            
            raw_data = latest_report.get('raw_excel_data', [])
            headers = latest_report.get('table_headers', [])
            
            if raw_data and headers:
                # 重建DataFrame
                df = rebuild_dataframe_with_headers(raw_data, headers)
                
                # 数据注入与计算
                df = inject_offline_and_calculate(df, st.session_state.offline_data)
                
                # 添加注释和序号
                df = add_meta_columns(df)
                
                # 样式应用
                st.dataframe(style_dataframe(df, "profit"), use_container_width=True, hide_index=True)
                
                # Excel下载 (基于新计算的数据)
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
                    df.to_excel(writer, index=False, sheet_name="报表")
                
                st.download_button(
                    label="📥 下载本期完整报表",
                    data=buffer.getvalue(),
                    file_name=f"{store_info['store_name']}_报表.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            
            if st.button("重新录入成本"):
                st.session_state.cost_submitted = False
                st.rerun()
                
        else:
            st.info("暂无报表数据")

def create_upload_app():
    """批量上传应用"""
    st.title("📤 批量上传系统")
    db_manager = get_db_manager()
    if not db_manager.is_connected(): return
    
    if 'admin_authenticated' not in st.session_state: st.session_state.admin_authenticated = False
    
    if not st.session_state.admin_authenticated:
        st.subheader("🔐 管理员登录")
        c1, c2, c3 = st.columns([1, 2, 1])
        with c2:
            pwd = st.text_input("密码", type="password")
            if st.button("登录"):
                if pwd == ConfigManager.get_admin_password():
                    st.session_state.admin_authenticated = True
                    st.rerun()
                else: st.error("密码错误")
        return
        
    db = db_manager.get_database()
    uploader = BulkReportUploader(db)
    
    col1, col2 = st.columns([2, 1])
    with col1:
        st.subheader("上传设置")
        report_month = st.text_input("报表月份", value=datetime.now().strftime("%Y-%m"))
        clear_history = st.checkbox("完全覆盖历史数据", value=True)
        uploaded_file = st.file_uploader("选择Excel文件", type=['xlsx', 'xls'])
        
        if uploaded_file and report_month and st.button("开始上传", type="primary"):
            progress_bar = st.progress(0)
            status_text = st.empty()
            def update(p, m): progress_bar.progress(p/100); status_text.text(m)
            
            result = uploader.process_excel_file(uploaded_file, report_month, clear_history, update)
            
            st.success(f"成功: {result['success_count']}, 失败: {result['failed_count']}")
            if result['errors']:
                with st.expander("错误详情"):
                    for e in result['errors']: st.error(e)

def create_permission_app():
    """权限与系统管理应用"""
    st.title("⚙️ 系统与权限管理")
    db_manager = get_db_manager()
    if not db_manager.is_connected(): return
    
    if 'perm_admin_authenticated' not in st.session_state: st.session_state.perm_admin_authenticated = False
    
    if not st.session_state.perm_admin_authenticated:
        st.subheader("🔐 管理员登录")
        c1, c2, c3 = st.columns([1, 2, 1])
        with c2:
            pwd = st.text_input("密码", type="password", key="perm_pass")
            if st.button("登录", key="perm_login"):
                if pwd == ConfigManager.get_admin_password():
                    st.session_state.perm_admin_authenticated = True
                    st.rerun()
                else: st.error("密码错误")
        return

    db = db_manager.get_database()
    permission_manager = PermissionManager(db)
    
    tab1, tab2, tab3 = st.tabs(["📤 权限表上传", "📋 权限配置", "🛠️ 系统设置与数据"])
    
    with tab1:
        st.subheader("上传权限表")
        up_file = st.file_uploader("Excel/CSV文件", type=['xlsx', 'csv'])
        if up_file and st.button("上传权限"):
            up_file.seek(0)
            res = permission_manager.upload_permission_table(up_file)
            if res['success']: st.success(f"处理成功: {res['processed']} 条")
            else: st.error(res['message'])
            
    with tab2:
        st.subheader("当前权限")
        perms = permission_manager.get_all_permissions()
        if perms:
            for p in perms:
                with st.expander(f"{p['query_code']} - {p['store_name']}"):
                    if st.button("删除", key=f"del_{p['query_code']}"):
                        permission_manager.delete_permission(p['query_code'])
                        st.rerun()
        else: st.info("无数据")

    with tab3:
        st.subheader("📄 财务指引文件")
        st.info("上传新的PDF将覆盖旧文件")
        pdf_up = st.file_uploader("上传指引PDF", type=['pdf'])
        if pdf_up and st.button("上传文件"):
            if db_manager.save_guide_pdf(pdf_up): st.success("✅ 文件已更新")
            else: st.error("上传失败")
            
        st.markdown("---")
        st.subheader("📥 线下成本数据导出")
        st.write("导出所有门店填写的线下成本明细（分Sheet展示）")
        
        if st.button("生成汇总Excel"):
            all_costs = db_manager.get_all_offline_costs()
            if not all_costs:
                st.warning("暂无数据")
            else:
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                    # 获取所有门店ID
                    store_ids = set(item['store_id'] for item in all_costs)
                    # 查找门店名称映射
                    store_map = {}
                    for sid in store_ids:
                        s = db['stores'].find_one({'_id': sid})
                        if s: store_map[sid] = s['store_name']
                    
                    # 转换为DataFrame并分组写入
                    flat_data = []
                    for r in all_costs:
                        d = r.get('data', {})
                        row = {
                            '门店': store_map.get(r['store_id'], r['store_id']),
                            '月份': r['month'],
                            '提交时间': r.get('updated_at'),
                            '人工工资': d.get('wages'),
                            '房租': d.get('rent'),
                            '水电': d.get('utilities'),
                            '耗材': d.get('consumables'),
                            '其他': d.get('others')
                        }
                        flat_data.append(row)
                    
                    df_all = pd.DataFrame(flat_data)
                    for store_name in df_all['门店'].unique():
                        df_store = df_all[df_all['门店'] == store_name]
                        # 清洗sheet名称
                        safe_name = "".join([c for c in str(store_name) if c.isalnum()])[:30]
                        df_store.to_excel(writer, sheet_name=safe_name, index=False)
                        
                st.download_button(
                    label="📥 点击下载汇总表",
                    data=output.getvalue(),
                    file_name=f"线下成本汇总_{datetime.now().strftime('%Y%m%d')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

def main():
    with st.sidebar:
        st.title("🏪 门店报表系统")
        app_choice = st.selectbox("功能模块", ["门店查询系统", "批量上传系统", "权限管理系统"])
        st.markdown("---")
        if get_db_manager().is_connected(): st.success("✅ 系统在线")
        else: st.error("❌ 离线")

    if app_choice == "门店查询系统": create_query_app()
    elif app_choice == "批量上传系统": create_upload_app()
    elif app_choice == "权限管理系统": create_permission_app()

if __name__ == "__main__":
    main()

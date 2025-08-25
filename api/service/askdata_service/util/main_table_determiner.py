import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class TableType(Enum):
    """表类型枚举"""
    FACT = "fact"  # 事实表
    DIMENSION = "dimension"  # 维度表
    MIXED = "mixed"  # 混合表


@dataclass
class TableScore:
    """表评分结果"""
    model_id: str
    model_name: str
    total_score: float
    relation_score: float
    metric_score: float
    dimension_score: float
    semantic_score: float
    reasons: List[str]


class MainTableDeterminer:
    """主表判断器"""
    
    def __init__(self):
        # 表名语义关键词权重
        self.semantic_keywords = {
            "fact": ["事实", "明细", "记录", "交易", "订单", "销售", "采购"],
            "measure": ["考核", "绩效", "评估", "统计", "汇总", "分析"],
            "dimension": ["维度", "字典", "基础", "信息", "部门", "产品", "客户"]
        }
        
        # 时间维度字段标识
        self.time_dimension_keywords = ["date", "time", "year", "month", "day", "季度", "年份", "日期", "时间"]
    
    def determine_main_table(
        self,
        dataset_detail: Dict,
        model_relationships: List[Dict],
        user_permissions: Optional[Dict] = None
    ) -> Tuple[str, TableScore]:
        """
        确定主表
        
        Args:
            dataset_detail: 数据集详情
            model_relationships: 模型关系列表
            user_permissions: 用户权限信息
            
        Returns:
            (主表ID, 评分详情)
        """
        models = dataset_detail.get("models", [])
        metrics = dataset_detail.get("metrics", [])
        dimensions = dataset_detail.get("dimensions", [])
        
        if not models:
            raise ValueError("数据集中没有模型")
        
        # 计算每个模型的得分
        scores = []
        for model in models:
            score = self._calculate_model_score(
                model, 
                models,
                metrics, 
                dimensions, 
                model_relationships
            )
            scores.append(score)
        
        # 根据总分排序
        scores.sort(key=lambda x: x.total_score, reverse=True)
        
        # 记录评分详情
        logger.info("=== 主表判断评分结果 ===")
        for idx, score in enumerate(scores):
            logger.info(f"排名 {idx + 1}: {score.model_name} (ID: {score.model_id})")
            logger.info(f"  总分: {score.total_score:.2f}")
            logger.info(f"  - 关联关系得分: {score.relation_score:.2f}")
            logger.info(f"  - 指标得分: {score.metric_score:.2f}")
            logger.info(f"  - 维度得分: {score.dimension_score:.2f}")
            logger.info(f"  - 语义得分: {score.semantic_score:.2f}")
            logger.info(f"  判断依据: {', '.join(score.reasons)}")
        
        # 返回得分最高的表作为主表
        main_table = scores[0]
        logger.info(f"\n最终确定主表: {main_table.model_name} (ID: {main_table.model_id})")
        
        return main_table.model_id, main_table
    
    def _calculate_model_score(
        self,
        model: Dict,
        all_models: List[Dict],
        metrics: List[Dict],
        dimensions: List[Dict],
        relationships: List[Dict]
    ) -> TableScore:
        """计算单个模型的得分"""
        model_id = model.get("modelId")
        model_name = model.get("modelName", "")
        table_name = model.get("tableName", "")
        
        reasons = []
        
        # 1. 关联关系评分 (权重30%)
        relation_score = self._calculate_relation_score(model_id, relationships)
        if relation_score > 20:
            reasons.append("作为关联起点")
        
        # 2. 指标评分 (权重40%)
        metric_score = self._calculate_metric_score(model_id, metrics)
        if metric_score > 30:
            reasons.append(f"包含{int(metric_score/10)}个指标")
        
        # 3. 维度评分 (权重20%)
        dimension_score = self._calculate_dimension_score(model_id, dimensions)
        if dimension_score > 15:
            reasons.append("包含时间维度")
        
        # 4. 语义评分 (权重10%)
        semantic_score = self._calculate_semantic_score(model_name, table_name)
        if semantic_score > 5:
            reasons.append("表名符合事实表特征")
        
        total_score = relation_score + metric_score + dimension_score + semantic_score
        
        return TableScore(
            model_id=model_id,
            model_name=model_name,
            total_score=total_score,
            relation_score=relation_score,
            metric_score=metric_score,
            dimension_score=dimension_score,
            semantic_score=semantic_score,
            reasons=reasons
        )
    
    def _calculate_relation_score(self, model_id: str, relationships: List[Dict]) -> float:
        """计算关联关系得分"""
        score = 0.0
        
        # 统计作为source和target的次数
        as_source = 0
        as_target = 0
        
        for rel in relationships:
            if rel.get("sourceModelId") == model_id:
                as_source += 1
            if rel.get("targetModelId") == model_id:
                as_target += 1
        
        # 只作为source不作为target，得满分
        if as_source > 0 and as_target == 0:
            score = 30.0
        # 既是source又是target，得部分分
        elif as_source > 0 and as_target > 0:
            score = 15.0 + (as_source - as_target) * 2.5
            score = max(0, min(25, score))  # 限制在0-25之间
        # 只作为target，不得分
        elif as_target > 0:
            score = 0.0
        # 孤立表，得少量分数
        else:
            score = 5.0
        
        return score
    
    def _calculate_metric_score(self, model_id: str, metrics: List[Dict]) -> float:
        """计算指标得分"""
        score = 0.0
        model_metrics = [m for m in metrics if m.get("modelId") == model_id]
        
        for metric in model_metrics:
            # 每个指标基础分10分
            score += 10.0
            
            # numeric类型额外加5分
            if metric.get("dataType") in ["numeric", "int", "float", "decimal", "number"]:
                score += 5.0
        
        # 最高40分
        return min(40.0, score)
    
    def _calculate_dimension_score(self, model_id: str, dimensions: List[Dict]) -> float:
        """计算维度得分"""
        score = 0.0
        model_dimensions = [d for d in dimensions if d.get("modelId") == model_id]
        
        for dim in model_dimensions:
            dim_name = dim.get("dimensionname", "").lower()
            dim_name_en = dim.get("dimname_en", "").lower()
            data_type = dim.get("dataType", "").lower()
            
            # 检查是否为时间维度
            is_time_dim = False
            for keyword in self.time_dimension_keywords:
                if keyword in dim_name or keyword in dim_name_en:
                    is_time_dim = True
                    break
            
            if not is_time_dim and data_type in ["date", "datetime", "timestamp"]:
                is_time_dim = True
            
            if is_time_dim:
                score += 20.0  # 时间维度直接给满分
                break
            
            # ID类维度
            if "id" in dim_name_en or "ID" in dim_name:
                score += 10.0
        
        # 最高20分
        return min(20.0, score)
    
    def _calculate_semantic_score(self, model_name: str, table_name: str) -> float:
        """计算语义得分"""
        score = 0.0
        combined_name = f"{model_name} {table_name}".lower()
        
        # 检查事实表关键词
        for keyword in self.semantic_keywords["fact"]:
            if keyword.lower() in combined_name:
                score += 3.0
        
        # 检查度量表关键词
        for keyword in self.semantic_keywords["measure"]:
            if keyword.lower() in combined_name:
                score += 5.0
        
        # 检查维度表关键词（减分）
        for keyword in self.semantic_keywords["dimension"]:
            if keyword.lower() in combined_name:
                score -= 2.0
        
        # 最高10分，最低0分
        return max(0.0, min(10.0, score))
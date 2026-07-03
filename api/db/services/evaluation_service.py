"""
@project: multirag
@file: evaluation_service.py
@desc: RAG Evaluation Service

Provides functionality for evaluating RAG system performance including:
- Dataset management
- Test case management
- Evaluation execution
- Metrics computation
- Configuration recommendations
"""

import asyncio
import logging
import queue
import threading
from datetime import datetime
from timeit import default_timer as timer
from typing import Any

from sqlalchemy.orm import Session

from api.db.db_models import EvaluationCase, EvaluationDataset, EvaluationResult, EvaluationRun
from api.db.services.common_service import CommonService
from api.db.services.dialog_service import DialogService
from common.constants import StatusEnum
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp


class EvaluationDatasetService(CommonService):
    """Service for managing evaluation datasets"""

    model = EvaluationDataset


class EvaluationCaseService(CommonService):
    """Service for managing evaluation test cases"""

    model = EvaluationCase


class EvaluationRunService(CommonService):
    """Service for managing evaluation runs"""

    model = EvaluationRun


class EvaluationResultService(CommonService):
    """Service for managing evaluation results"""

    model = EvaluationResult


class EvaluationService:
    """Main service for RAG evaluations"""

    # ==================== Dataset Management ====================

    @classmethod
    def create_dataset(cls, db: Session, name: str, description: str, kb_ids: list[str], tenant_id: str, user_id: str) -> tuple[bool, str]:
        """
        Create a new evaluation dataset.

        Args:
            db: Database session
            name: Dataset name
            description: Dataset description
            kb_ids: List of knowledge base IDs to evaluate against
            tenant_id: Tenant ID
            user_id: User ID who creates the dataset

        Returns:
            (success, dataset_id or error_message)
        """
        try:
            dataset_id = get_uuid()
            EvaluationDatasetService.insert(db, id=dataset_id, tenant_id=tenant_id, name=name, description=description, kb_ids=kb_ids, created_by=user_id, status=StatusEnum.VALID.value)
            return True, dataset_id
        except Exception as e:
            logging.error(f"Error creating evaluation dataset: {e}")
            return False, str(e)

    @classmethod
    def get_dataset(cls, db: Session, dataset_id: str) -> dict[str, Any] | None:
        """Get dataset by ID"""
        try:
            dataset = EvaluationDatasetService.get_by_id(db, dataset_id)
            if dataset:
                return dataset.to_dict()
            return None
        except Exception as e:
            logging.error(f"Error getting dataset {dataset_id}: {e}")
            return None

    @classmethod
    def list_datasets(cls, db: Session, tenant_id: str, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        """List datasets for a tenant"""
        try:
            query = db.query(EvaluationDataset).filter((EvaluationDataset.tenant_id == tenant_id) & (EvaluationDataset.status == StatusEnum.VALID.value)).order_by(EvaluationDataset.create_time.desc())

            total = query.count()
            datasets = query.offset((page - 1) * page_size).limit(page_size).all()

            return {"total": total, "datasets": [d.to_dict() for d in datasets]}
        except Exception as e:
            logging.error(f"Error listing datasets: {e}")
            return {"total": 0, "datasets": []}

    @classmethod
    def update_dataset(cls, db: Session, dataset_id: str, **kwargs) -> bool:
        """Update dataset"""
        try:
            return EvaluationDatasetService.update_by_id(db, dataset_id, kwargs) > 0
        except Exception as e:
            logging.error(f"Error updating dataset {dataset_id}: {e}")
            return False

    @classmethod
    def delete_dataset(cls, db: Session, dataset_id: str) -> bool:
        """Soft delete dataset"""
        try:
            return EvaluationDatasetService.update_by_id(db, dataset_id, {"status": StatusEnum.INVALID.value}) > 0
        except Exception as e:
            logging.error(f"Error deleting dataset {dataset_id}: {e}")
            return False

    # ==================== Test Case Management ====================

    @classmethod
    def add_test_case(
        cls,
        db: Session,
        dataset_id: str,
        question: str,
        reference_answer: str | None = None,
        relevant_doc_ids: list[str] | None = None,
        relevant_chunk_ids: list[str] | None = None,
        case_metadata: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """
        Add a test case to a dataset.

        Args:
            db: Database session
            dataset_id: Dataset ID
            question: Test question
            reference_answer: Optional ground truth answer
            relevant_doc_ids: Optional list of relevant document IDs
            relevant_chunk_ids: Optional list of relevant chunk IDs
            case_metadata: Optional additional metadata

        Returns:
            (success, case_id or error_message)
        """
        try:
            case_id = get_uuid()
            EvaluationCaseService.insert(
                db,
                id=case_id,
                dataset_id=dataset_id,
                question=question,
                reference_answer=reference_answer,
                relevant_doc_ids=relevant_doc_ids,
                relevant_chunk_ids=relevant_chunk_ids,
                case_metadata=case_metadata,
            )
            return True, case_id
        except Exception as e:
            logging.error(f"Error adding test case: {e}")
            return False, str(e)

    @classmethod
    def get_test_cases(cls, db: Session, dataset_id: str) -> list[dict[str, Any]]:
        """Get all test cases for a dataset"""
        try:
            cases = db.query(EvaluationCase).filter(EvaluationCase.dataset_id == dataset_id).order_by(EvaluationCase.create_time).all()
            return [c.to_dict() for c in cases]
        except Exception as e:
            logging.error(f"Error getting test cases for dataset {dataset_id}: {e}")
            return []

    @classmethod
    def delete_test_case(cls, db: Session, case_id: str) -> bool:
        """Delete a test case"""
        try:
            return EvaluationCaseService.delete_by_id(db, case_id) > 0
        except Exception as e:
            logging.error(f"Error deleting test case {case_id}: {e}")
            return False

    @classmethod
    def import_test_cases(cls, db: Session, dataset_id: str, cases: list[dict[str, Any]]) -> tuple[int, int]:
        """
        Bulk import test cases from a list.

        Args:
            db: Database session
            dataset_id: Dataset ID
            cases: List of test case dictionaries

        Returns:
            (success_count, failure_count)
        """
        success_count = 0
        failure_count = 0
        case_instances = []

        if not cases:
            return success_count, failure_count

        cur_timestamp = current_timestamp()
        try:
            for case_data in cases:
                case_id = get_uuid()
                case_info = {
                    "id": case_id,
                    "dataset_id": dataset_id,
                    "question": case_data.get("question", ""),
                    "reference_answer": case_data.get("reference_answer"),
                    "relevant_doc_ids": case_data.get("relevant_doc_ids"),
                    "relevant_chunk_ids": case_data.get("relevant_chunk_ids"),
                    "metadata": case_data.get("metadata"),
                    "create_time": cur_timestamp,
                }

                case_instances.append(EvaluationCase(**case_info))
            EvaluationCase.bulk_create(case_instances, batch_size=300)
            success_count = len(case_instances)
            failure_count = 0

        except Exception as e:
            logging.error(f"Error bulk importing test cases: {e!s}")
            failure_count = len(cases)
            success_count = 0

        return success_count, failure_count

    # ==================== Evaluation Execution ====================

    @classmethod
    def start_evaluation(cls, db: Session, dataset_id: str, dialog_id: str, user_id: str, name: str | None = None) -> tuple[bool, str]:
        """
        Start an evaluation run.

        Args:
            db: Database session
            dataset_id: Dataset ID
            dialog_id: Dialog configuration to evaluate
            user_id: User ID who starts the run
            name: Optional run name

        Returns:
            (success, run_id or error_message)
        """
        try:
            # Get dialog configuration
            dialog = DialogService.get_by_id(db, dialog_id)
            if not dialog:
                return False, "Dialog not found"

            # Create evaluation run
            run_id = get_uuid()
            if not name:
                name = f"Evaluation Run {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

            config_snapshot = {
                "id": dialog.id,
                "name": dialog.name,
                "llm_id": dialog.llm_id,
                "llm_setting": dialog.llm_setting,
                "prompt_config": dialog.prompt_config,
                "similarity_threshold": dialog.similarity_threshold,
                "vector_similarity_weight": dialog.vector_similarity_weight,
                "top_n": dialog.top_n,
                "top_k": dialog.top_k,
                "rerank_id": dialog.rerank_id,
                "kb_ids": dialog.kb_ids,
            }

            EvaluationRunService.insert(
                db,
                id=run_id,
                dataset_id=dataset_id,
                dialog_id=dialog_id,
                name=name,
                config_snapshot=config_snapshot,
                metrics_summary=None,
                run_status="RUNNING",
                created_by=user_id,
                complete_time=None,
            )

            # Execute evaluation (in a background task for production)
            cls._execute_evaluation(db, run_id, dataset_id, dialog)

            return True, run_id
        except Exception as e:
            logging.error(f"Error starting evaluation: {e}")
            return False, str(e)

    @classmethod
    def _execute_evaluation(cls, db: Session, run_id: str, dataset_id: str, dialog: Any):
        """
        Execute evaluation for all test cases.

        This method runs the RAG pipeline for each test case and computes metrics.
        """
        try:
            # Get all test cases
            test_cases = cls.get_test_cases(db, dataset_id)

            if not test_cases:
                EvaluationRunService.update_by_id(db, run_id, {"run_status": "FAILED", "complete_time": current_timestamp()})
                return

            # Execute each test case
            results = []
            for case in test_cases:
                result = cls._evaluate_single_case(db, run_id, case, dialog)
                if result:
                    results.append(result)

            # Compute summary metrics
            metrics_summary = cls._compute_summary_metrics(results)

            # Update run status
            EvaluationRunService.update_by_id(db, run_id, {"run_status": "COMPLETED", "metrics_summary": metrics_summary, "complete_time": current_timestamp()})

        except Exception as e:
            logging.error(f"Error executing evaluation {run_id}: {e}")
            EvaluationRunService.update_by_id(db, run_id, {"run_status": "FAILED", "complete_time": current_timestamp()})

    @classmethod
    def _evaluate_single_case(cls, db: Session, run_id: str, case: dict[str, Any], dialog: Any) -> dict[str, Any] | None:
        """
        Evaluate a single test case.

        Args:
            db: Database session
            run_id: Evaluation run ID
            case: Test case dictionary
            dialog: Dialog configuration

        Returns:
            Result dictionary or None if failed
        """
        try:
            # Prepare messages
            messages = [{"role": "user", "content": case["question"]}]

            # Execute RAG pipeline
            start_time = timer()
            answer = ""
            retrieved_chunks = []

            # 使用局部桥接函数调用异步 chat
            def _sync_from_async_gen(async_gen):
                result_queue: queue.Queue = queue.Queue()

                def runner():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    async def consume():
                        try:
                            async for item in async_gen:
                                result_queue.put(item)
                        except Exception as e:
                            result_queue.put(e)
                        finally:
                            result_queue.put(StopIteration)

                    loop.run_until_complete(consume())
                    loop.close()

                threading.Thread(target=runner, daemon=True).start()

                while True:
                    item = result_queue.get()
                    if item is StopIteration:
                        break
                    if isinstance(item, Exception):
                        raise item
                    yield item

            def chat(dialog, messages, stream=True, **kwargs):
                from api.db.services.dialog_service import async_chat

                return _sync_from_async_gen(async_chat(dialog, messages, db, stream=stream, **kwargs))

            for ans in chat(dialog, messages, stream=False):
                if isinstance(ans, dict):
                    answer = ans.get("answer", "")
                    retrieved_chunks = ans.get("reference", {}).get("chunks", [])
                    break

            execution_time = timer() - start_time

            # Compute metrics
            metrics = cls._compute_metrics(
                question=case["question"],
                generated_answer=answer,
                reference_answer=case.get("reference_answer"),
                retrieved_chunks=retrieved_chunks,
                relevant_chunk_ids=case.get("relevant_chunk_ids"),
            )

            # Save result
            result_id = get_uuid()
            result = {
                "id": result_id,
                "run_id": run_id,
                "case_id": case["id"],
                "generated_answer": answer,
                "retrieved_chunks": retrieved_chunks,
                "metrics": metrics,
                "execution_time": execution_time,
                "token_usage": None,  # TODO: Track token usage
            }

            EvaluationResultService.insert(db, **result)

            return result
        except Exception as e:
            logging.error(f"Error evaluating case {case.get('id')}: {e}")
            return None

    @classmethod
    def _compute_metrics(
        cls,
        question: str,
        generated_answer: str,
        reference_answer: str | None,
        retrieved_chunks: list[dict[str, Any]],
        relevant_chunk_ids: list[str] | None,
    ) -> dict[str, float]:
        """
        Compute evaluation metrics for a single test case.

        Returns:
            Dictionary of metric names to values
        """
        metrics = {}

        # Retrieval metrics (if ground truth chunks provided)
        if relevant_chunk_ids:
            retrieved_ids = [c.get("chunk_id") for c in retrieved_chunks]
            metrics.update(cls._compute_retrieval_metrics(retrieved_ids, relevant_chunk_ids))

        # Generation metrics
        if generated_answer:
            # Basic metrics
            metrics["answer_length"] = len(generated_answer)
            metrics["has_answer"] = 1.0 if generated_answer.strip() else 0.0

            # TODO: Implement advanced metrics using LLM-as-judge
            # - Faithfulness (hallucination detection)
            # - Answer relevance
            # - Context relevance
            # - Semantic similarity (if reference answer provided)

        return metrics

    @classmethod
    def _compute_retrieval_metrics(cls, retrieved_ids: list[str], relevant_ids: list[str]) -> dict[str, float]:
        """
        Compute retrieval metrics.

        Args:
            retrieved_ids: List of retrieved chunk IDs
            relevant_ids: List of relevant chunk IDs (ground truth)

        Returns:
            Dictionary of retrieval metrics
        """
        if not relevant_ids:
            return {}

        retrieved_set = set(retrieved_ids)
        relevant_set = set(relevant_ids)

        # Precision: proportion of retrieved that are relevant
        precision = len(retrieved_set & relevant_set) / len(retrieved_set) if retrieved_set else 0.0

        # Recall: proportion of relevant that were retrieved
        recall = len(retrieved_set & relevant_set) / len(relevant_set) if relevant_set else 0.0

        # F1 score
        f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0

        # Hit rate: whether any relevant chunk was retrieved
        hit_rate = 1.0 if (retrieved_set & relevant_set) else 0.0

        # MRR (Mean Reciprocal Rank): position of first relevant chunk
        mrr = 0.0
        for i, chunk_id in enumerate(retrieved_ids, 1):
            if chunk_id in relevant_set:
                mrr = 1.0 / i
                break

        return {"precision": precision, "recall": recall, "f1_score": f1, "hit_rate": hit_rate, "mrr": mrr}

    @classmethod
    def _compute_summary_metrics(cls, results: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Compute summary metrics across all test cases.

        Args:
            results: List of result dictionaries

        Returns:
            Summary metrics dictionary
        """
        if not results:
            return {}

        # Aggregate metrics
        metric_sums: dict[str, float] = {}
        metric_counts: dict[str, int] = {}

        for result in results:
            metrics = result.get("metrics", {})
            for key, value in metrics.items():
                if isinstance(value, (int, float)):
                    metric_sums[key] = metric_sums.get(key, 0) + value
                    metric_counts[key] = metric_counts.get(key, 0) + 1

        # Compute averages
        summary: dict[str, Any] = {"total_cases": len(results), "avg_execution_time": sum(r.get("execution_time", 0) for r in results) / len(results)}

        for key in metric_sums:
            summary[f"avg_{key}"] = metric_sums[key] / metric_counts[key]

        return summary

    # ==================== Results & Analysis ====================

    @classmethod
    def get_run_results(cls, db: Session, run_id: str) -> dict[str, Any]:
        """Get results for an evaluation run"""
        try:
            run = EvaluationRunService.get_by_id(db, run_id)
            if not run:
                return {}

            results = db.query(EvaluationResult).filter(EvaluationResult.run_id == run_id).order_by(EvaluationResult.create_time).all()

            return {"run": run.to_dict(), "results": [r.to_dict() for r in results]}
        except Exception as e:
            logging.error(f"Error getting run results {run_id}: {e}")
            return {}

    @classmethod
    def list_runs(cls, db: Session, tenant_id: str, dataset_id: str | None = None, dialog_id: str | None = None, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        """List evaluation runs"""
        try:
            query = db.query(EvaluationRun).join(EvaluationDataset, EvaluationRun.dataset_id == EvaluationDataset.id).filter(EvaluationDataset.tenant_id == tenant_id)

            if dataset_id:
                query = query.filter(EvaluationRun.dataset_id == dataset_id)
            if dialog_id:
                query = query.filter(EvaluationRun.dialog_id == dialog_id)

            query = query.order_by(EvaluationRun.create_time.desc())

            total = query.count()
            runs = query.offset((page - 1) * page_size).limit(page_size).all()

            return {"total": total, "runs": [r.to_dict() for r in runs]}
        except Exception as e:
            logging.error(f"Error listing runs: {e}")
            return {"total": 0, "runs": []}

    @classmethod
    def delete_run(cls, db: Session, run_id: str) -> bool:
        """Delete an evaluation run and its results"""
        try:
            # Delete results first
            db.query(EvaluationResult).filter(EvaluationResult.run_id == run_id).delete(synchronize_session=False)

            # Delete run
            EvaluationRunService.delete_by_id(db, run_id)
            db.commit()
            return True
        except Exception as e:
            logging.error(f"Error deleting run {run_id}: {e}")
            db.rollback()
            return False

    @classmethod
    def get_recommendations(cls, db: Session, run_id: str) -> list[dict[str, Any]]:
        """
        Analyze evaluation results and provide configuration recommendations.

        Args:
            db: Database session
            run_id: Evaluation run ID

        Returns:
            List of recommendation dictionaries
        """
        try:
            run = EvaluationRunService.get_by_id(db, run_id)
            if not run or not run.metrics_summary:
                return []

            metrics = run.metrics_summary
            recommendations = []

            # Low precision: retrieving irrelevant chunks
            if metrics.get("avg_precision", 1.0) < 0.7:
                recommendations.append(
                    {
                        "issue": "Low Precision",
                        "severity": "high",
                        "description": "System is retrieving many irrelevant chunks",
                        "suggestions": ["Increase similarity_threshold to filter out less relevant chunks", "Enable reranking to improve chunk ordering", "Reduce top_k to return fewer chunks"],
                    }
                )

            # Low recall: missing relevant chunks
            if metrics.get("avg_recall", 1.0) < 0.7:
                recommendations.append(
                    {
                        "issue": "Low Recall",
                        "severity": "high",
                        "description": "System is missing relevant chunks",
                        "suggestions": [
                            "Increase top_k to retrieve more chunks",
                            "Lower similarity_threshold to be more inclusive",
                            "Enable hybrid search (keyword + semantic)",
                            "Check chunk size - may be too large or too small",
                        ],
                    }
                )

            # Low F1 score
            if metrics.get("avg_f1_score", 1.0) < 0.5:
                recommendations.append(
                    {
                        "issue": "Low F1 Score",
                        "severity": "high",
                        "description": "Poor balance between precision and recall",
                        "suggestions": ["Review knowledge base content quality", "Consider adjusting chunk overlap", "Try different embedding models"],
                    }
                )

            # Low hit rate
            if metrics.get("avg_hit_rate", 1.0) < 0.8:
                recommendations.append(
                    {
                        "issue": "Low Hit Rate",
                        "severity": "medium",
                        "description": "Often failing to retrieve any relevant chunks",
                        "suggestions": ["Increase top_k significantly", "Review knowledge base coverage", "Consider query expansion or rewriting"],
                    }
                )

            # Slow response time
            if metrics.get("avg_execution_time", 0) > 5.0:
                recommendations.append(
                    {
                        "issue": "Slow Response Time",
                        "severity": "medium",
                        "description": f"Average response time is {metrics['avg_execution_time']:.2f}s",
                        "suggestions": ["Reduce top_k to retrieve fewer chunks", "Optimize embedding model selection", "Consider caching frequently asked questions"],
                    }
                )

            return recommendations
        except Exception as e:
            logging.error(f"Error generating recommendations for run {run_id}: {e}")
            return []

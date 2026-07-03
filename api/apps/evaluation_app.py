"""
@project: multirag
@file: evaluation_app.py
@desc: RAG Evaluation API Endpoints

Provides REST API for RAG evaluation functionality including:
- Dataset management
- Test case management
- Evaluation execution
- Results retrieval
- Configuration recommendations
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.evaluation_service import EvaluationService

router = APIRouter()


# ==================== Pydantic Models ====================


class CreateDatasetRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Dataset name")
    description: str = Field(default="", description="Dataset description")
    kb_ids: list[str] = Field(..., min_items=1, description="Knowledge base IDs")


class UpdateDatasetRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255, description="Dataset name")
    description: str | None = Field(default=None, description="Dataset description")
    kb_ids: list[str] | None = Field(default=None, description="Knowledge base IDs")


class AddTestCaseRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Test question")
    reference_answer: str | None = Field(default=None, description="Ground truth answer")
    relevant_doc_ids: list[str] | None = Field(default=None, description="Relevant document IDs")
    relevant_chunk_ids: list[str] | None = Field(default=None, description="Relevant chunk IDs")
    case_metadata: dict | None = Field(default=None, description="Additional metadata")


class ImportTestCasesRequest(BaseModel):
    cases: list[AddTestCaseRequest] = Field(..., min_items=1, description="Test cases to import")


class StartEvaluationRequest(BaseModel):
    dataset_id: str = Field(..., description="Dataset ID")
    dialog_id: str = Field(..., description="Dialog ID to evaluate")
    name: str | None = Field(default=None, description="Optional run name")


class CompareRunsRequest(BaseModel):
    run_ids: list[str] = Field(..., min_items=2, description="Run IDs to compare")


# ==================== Dataset Management ====================


@router.post("/dataset/create", summary="Create evaluation dataset")
def create_dataset(request: CreateDatasetRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    Create a new evaluation dataset.

    - **name**: Dataset name
    - **description**: Optional description
    - **kb_ids**: List of knowledge base IDs to evaluate against
    """
    success, result = EvaluationService.create_dataset(db=db, name=request.name.strip(), description=request.description, kb_ids=request.kb_ids, tenant_id=user.id, user_id=user.id)

    if not success:
        raise HTTPException(status_code=400, detail=result)

    return {"code": 0, "message": "success", "data": {"dataset_id": result}}


@router.get("/dataset/list", summary="List evaluation datasets")
def list_datasets(page: int = 1, page_size: int = 20, db: Session = Depends(get_db), user=Depends(manager)):
    """
    List evaluation datasets for current tenant.

    - **page**: Page number (default: 1)
    - **page_size**: Items per page (default: 20)
    """
    result = EvaluationService.list_datasets(db=db, tenant_id=user.id, page=page, page_size=page_size)
    return {"code": 0, "message": "success", "data": result}


@router.get("/dataset/{dataset_id}", summary="Get dataset details")
def get_dataset(dataset_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """Get dataset details by ID"""
    dataset = EvaluationService.get_dataset(db, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")

    return {"code": 0, "message": "success", "data": dataset}


@router.put("/dataset/{dataset_id}", summary="Update dataset")
def update_dataset(dataset_id: str, request: UpdateDatasetRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    Update dataset.

    - **name**: New name
    - **description**: New description
    - **kb_ids**: New knowledge base IDs
    """
    update_data = request.model_dump(exclude_none=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No update data provided")

    success = EvaluationService.update_dataset(db, dataset_id, **update_data)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to update dataset")

    return {"code": 0, "message": "success", "data": {"dataset_id": dataset_id}}


@router.delete("/dataset/{dataset_id}", summary="Delete dataset")
def delete_dataset(dataset_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """Delete dataset (soft delete)"""
    success = EvaluationService.delete_dataset(db, dataset_id)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to delete dataset")

    return {"code": 0, "message": "success", "data": {"dataset_id": dataset_id}}


# ==================== Test Case Management ====================


@router.post("/dataset/{dataset_id}/case/add", summary="Add test case")
def add_test_case(dataset_id: str, request: AddTestCaseRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    Add a test case to a dataset.

    - **question**: Test question
    - **reference_answer**: Optional ground truth answer
    - **relevant_doc_ids**: Optional list of relevant document IDs
    - **relevant_chunk_ids**: Optional list of relevant chunk IDs
    - **case_metadata**: Optional additional metadata
    """
    success, result = EvaluationService.add_test_case(
        db=db,
        dataset_id=dataset_id,
        question=request.question.strip(),
        reference_answer=request.reference_answer,
        relevant_doc_ids=request.relevant_doc_ids,
        relevant_chunk_ids=request.relevant_chunk_ids,
        case_metadata=request.case_metadata,
    )

    if not success:
        raise HTTPException(status_code=400, detail=result)

    return {"code": 0, "message": "success", "data": {"case_id": result}}


@router.post("/dataset/{dataset_id}/case/import", summary="Bulk import test cases")
def import_test_cases(dataset_id: str, request: ImportTestCasesRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    Bulk import test cases.

    - **cases**: List of test cases to import
    """
    cases_data = [case.model_dump() for case in request.cases]
    success_count, failure_count = EvaluationService.import_test_cases(db=db, dataset_id=dataset_id, cases=cases_data)

    return {"code": 0, "message": "success", "data": {"success_count": success_count, "failure_count": failure_count, "total": len(request.cases)}}


@router.get("/dataset/{dataset_id}/cases", summary="Get test cases")
def get_test_cases(dataset_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """Get all test cases for a dataset"""
    cases = EvaluationService.get_test_cases(db, dataset_id)
    return {"code": 0, "message": "success", "data": {"cases": cases, "total": len(cases)}}


@router.delete("/case/{case_id}", summary="Delete test case")
def delete_test_case(case_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """Delete a test case"""
    success = EvaluationService.delete_test_case(db, case_id)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to delete test case")

    return {"code": 0, "message": "success", "data": {"case_id": case_id}}


# ==================== Evaluation Execution ====================


@router.post("/run/start", summary="Start evaluation run")
def start_evaluation(request: StartEvaluationRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    Start an evaluation run.

    - **dataset_id**: Dataset ID
    - **dialog_id**: Dialog configuration to evaluate
    - **name**: Optional run name
    """
    success, result = EvaluationService.start_evaluation(db=db, dataset_id=request.dataset_id, dialog_id=request.dialog_id, user_id=user.id, name=request.name)

    if not success:
        raise HTTPException(status_code=400, detail=result)

    return {"code": 0, "message": "success", "data": {"run_id": result}}


@router.get("/run/{run_id}", summary="Get evaluation run")
def get_evaluation_run(run_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """Get evaluation run details"""
    result = EvaluationService.get_run_results(db, run_id)
    if not result:
        raise HTTPException(status_code=404, detail="Evaluation run not found")

    return {"code": 0, "message": "success", "data": result}


@router.get("/run/{run_id}/results", summary="Get run results")
def get_run_results(run_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """Get detailed results for an evaluation run"""
    result = EvaluationService.get_run_results(db, run_id)
    if not result:
        raise HTTPException(status_code=404, detail="Evaluation run not found")

    return {"code": 0, "message": "success", "data": result}


@router.get("/run/list", summary="List evaluation runs")
def list_evaluation_runs(dataset_id: str | None = None, dialog_id: str | None = None, page: int = 1, page_size: int = 20, db: Session = Depends(get_db), user=Depends(manager)):
    """
    List evaluation runs.

    - **dataset_id**: Filter by dataset (optional)
    - **dialog_id**: Filter by dialog (optional)
    - **page**: Page number (default: 1)
    - **page_size**: Items per page (default: 20)
    """
    result = EvaluationService.list_runs(db=db, tenant_id=user.id, dataset_id=dataset_id, dialog_id=dialog_id, page=page, page_size=page_size)
    return {"code": 0, "message": "success", "data": result}


@router.delete("/run/{run_id}", summary="Delete evaluation run")
def delete_evaluation_run(run_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """Delete an evaluation run"""
    success = EvaluationService.delete_run(db, run_id)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to delete evaluation run")

    return {"code": 0, "message": "success", "data": {"run_id": run_id}}


# ==================== Analysis & Recommendations ====================


@router.get("/run/{run_id}/recommendations", summary="Get recommendations")
def get_recommendations(run_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """Get configuration recommendations based on evaluation results"""
    recommendations = EvaluationService.get_recommendations(db, run_id)
    return {"code": 0, "message": "success", "data": {"recommendations": recommendations}}


@router.post("/compare", summary="Compare evaluation runs")
def compare_runs(request: CompareRunsRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    Compare multiple evaluation runs.

    - **run_ids**: List of run IDs to compare (minimum 2)
    """
    comparison = {}
    for run_id in request.run_ids:
        result = EvaluationService.get_run_results(db, run_id)
        if result:
            comparison[run_id] = {
                "name": result["run"].get("name"),
                "metrics_summary": result["run"].get("metrics_summary"),
                "config_snapshot": result["run"].get("config_snapshot"),
                "create_time": result["run"].get("create_time"),
            }

    return {"code": 0, "message": "success", "data": {"comparison": comparison}}


@router.get("/run/{run_id}/export", summary="Export results")
def export_results(run_id: str, format: str = "json", db: Session = Depends(get_db), user=Depends(manager)):
    """Export evaluation results as JSON/CSV"""
    result = EvaluationService.get_run_results(db, run_id)
    if not result:
        raise HTTPException(status_code=404, detail="Evaluation run not found")

    # TODO: Implement CSV export
    return {"code": 0, "message": "success", "data": result}

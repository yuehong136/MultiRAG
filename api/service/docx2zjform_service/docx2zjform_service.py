from docx.document import Document as DocumentType
from api.service.docx2zjform_service.document_processor import DocumentProcessor
from api.service.docx2zjform_service.element import DocumentParser
from api.service.docx2zjform_service.component import Component


class Docx2ZJFormService:
    @staticmethod
    async def convert(doc: DocumentType, db, user_id) -> str:
        elements = await DocumentParser.parse(doc)
        processor = DocumentProcessor()
        components = processor.process(elements=elements)

        component_json_str = Component.components_to_json_string(components)
        return component_json_str

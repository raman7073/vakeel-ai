from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class Contract(BaseModel):
    id: Optional[str] = None
    filename: str
    original_name: str
    upload_date: str = ""
    text_content: str = ""
    page_count: int = 0
    word_count: int = 0
    status: str = "uploaded"

    def model_post_init(self, context):
        if not self.upload_date:
            self.upload_date = datetime.now().isoformat()

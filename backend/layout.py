from pydantic import BaseModel, Field, model_validator


class CropRect(BaseModel):
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)
    width: float = Field(ge=.02, le=1, allow_inf_nan=False)
    height: float = Field(ge=.02, le=1, allow_inf_nan=False)

    @model_validator(mode='after')
    def inside_source(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError('Crop area must stay inside the source frame')
        return self


class DualCrop(BaseModel):
    face: CropRect
    gameplay: CropRect
    top_fraction: float = Field(default=.3, ge=.2, le=.6, allow_inf_nan=False)
    face_on_top: bool = True

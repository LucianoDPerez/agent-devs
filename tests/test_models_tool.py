"""Tests para inspect_models: Prisma, SQLAlchemy, Django, TypeORM, Rails."""

import tempfile
from pathlib import Path

from tools.models import inspect_models


def _create_repo(files: dict[str, str]) -> str:
    tmp = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(tmp) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp


class TestPrisma:
    def test_model_con_tabla_y_relaciones(self):
        repo = _create_repo({
            "backend/prisma/schema.prisma": """
model Patient {
  id        String   @id @default(cuid())
  name      String
  consultas Consulta[]
  @@map("patients")
}

model Consulta {
  id        String  @id
  patientId String
  patient   Patient @relation(fields: [patientId], references: [id])
}
""",
        })
        result = inspect_models.invoke({"path": repo})
        assert "PRISMA" in result
        assert "Patient — tabla: patients" in result
        assert "Consulta — 3 campos → Patient" in result


class TestSQLAlchemy:
    def test_class_base_con_columnas_y_relacion(self):
        repo = _create_repo({
            "app/models/user.py": """
from sqlalchemy import Column, Integer, String, relationship

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String(80))
    posts = relationship("Post")
""",
        })
        result = inspect_models.invoke({"path": repo})
        assert "User — tabla: users — 2 campos → Post" in result


class TestDjango:
    def test_models_model_con_fk(self):
        repo = _create_repo({
            "app/models.py": """
from django.db import models

class Consulta(models.Model):
    paciente = models.ForeignKey(Paciente, on_delete=models.CASCADE)
    fecha = models.DateField()
""",
        })
        result = inspect_models.invoke({"path": repo})
        assert "Consulta — 2 campos → Paciente" in result


class TestTypeORM:
    def test_entity_con_relaciones(self):
        repo = _create_repo({
            "src/entity/photo.entity.ts": """
import { Entity, Column, ManyToOne } from "typeorm";
import { User } from "./user.entity";

@Entity()
export class Photo {
  @Column()
  url: string;

  @ManyToOne(() => User, (user) => user.photos)
  user: User;
}
""",
        })
        result = inspect_models.invoke({"path": repo})
        assert "TYPEORM" in result
        assert "Photo — 1 campos → User" in result


class TestRails:
    def test_schema_rb_tables(self):
        repo = _create_repo({
            "db/schema.rb": """
create_table "users", force: :cascade do |t|
end
create_table "posts", force: :cascade do |t|
end
""",
        })
        result = inspect_models.invoke({"path": repo})
        assert 'users (tabla)' in result
        assert 'posts (tabla)' in result

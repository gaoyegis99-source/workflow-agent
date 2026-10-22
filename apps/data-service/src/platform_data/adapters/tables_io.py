"""Bounded, explicit tabular conversion; never infer CSV column types."""

import csv
import io

import pyarrow as pa
import pyarrow.csv as pc
import pyarrow.parquet as pq
from platform_contracts.service import ensure

from platform_data.domain.model import MAX_COLUMNS, MAX_ROWS

TYPES = {"string": pa.string(), "int64": pa.int64(), "float64": pa.float64(), "boolean": pa.bool_()}


def normalize(content, format, config):
    if format == "csv":
        text = content.decode("utf-8-sig", errors="strict")
        header = next(csv.reader(io.StringIO(text)), [])
        ensure(
            0 < len(header) <= MAX_COLUMNS and len(set(header)) == len(header),
            "MALFORMED_REQUEST",
            "CSV requires unique columns within the column limit",
            422,
        )
        ensure(
            [col["name"] for col in config["columns"]] == header,
            "MALFORMED_REQUEST",
            "Explicit CSV columns must match the header in order",
            422,
        )
        table = pc.read_csv(
            pa.BufferReader(text.encode()),
            convert_options=pc.ConvertOptions(
                column_types={col["name"]: TYPES[col["type"]] for col in config["columns"]},
                null_values=config["null_values"],
                strings_can_be_null=False,
            ),
        )
    else:
        source = pq.ParquetFile(pa.BufferReader(content))
        ensure(source.metadata.num_rows <= MAX_ROWS, "MALFORMED_REQUEST", "Row limit exceeded", 422)
        ensure(
            0 < len(source.schema_arrow) <= MAX_COLUMNS,
            "MALFORMED_REQUEST",
            "Column limit exceeded",
            422,
        )
        ensure(
            all(
                not (
                    pa.types.is_nested(field.type)
                    or pa.types.is_binary(field.type)
                    or pa.types.is_large_binary(field.type)
                )
                for field in source.schema_arrow
            ),
            "MALFORMED_REQUEST",
            "Only flat scalar Parquet fields are supported",
            422,
        )
        table = source.read()
    ensure(
        table.num_rows <= MAX_ROWS and 0 < table.num_columns <= MAX_COLUMNS,
        "MALFORMED_REQUEST",
        "Table limit exceeded",
        422,
    )
    fields = [
        {"field_id": f"f{i}", "name": name, "type": str(table.schema.field(i).type)}
        for i, name in enumerate(table.column_names)
    ]
    table = table.rename_columns([f"f{i}" for i in range(table.num_columns)])
    output = io.BytesIO()
    pq.write_table(table, output, compression="snappy", version="2.6")
    return output.getvalue(), fields, table.num_rows

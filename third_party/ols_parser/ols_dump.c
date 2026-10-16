#include "ols.h"

#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void set_err(char *err, size_t err_len, const char *path, const char *msg)
{
	if (err && err_len)
		snprintf(err, err_len, "%s: %s", path, msg);
}

static void *slurp(const char *path, size_t *out_len, char *err, size_t err_len)
{
	FILE *f;
	long n;
	void *buf;

	f = fopen(path, "rb");
	if (!f) {
		set_err(err, err_len, path, strerror(errno));
		return NULL;
	}
	if (fseek(f, 0, SEEK_END) < 0 || (n = ftell(f)) < 0) {
		set_err(err, err_len, path, "failed to determine file size");
		fclose(f);
		return NULL;
	}
	rewind(f);
	buf = malloc(n > 0 ? (size_t)n : 1);
	if (!buf) {
		set_err(err, err_len, path, "out of memory");
		fclose(f);
		return NULL;
	}
	if (n > 0 && fread(buf, 1, (size_t)n, f) != (size_t)n) {
		set_err(err, err_len, path, "failed to read whole file");
		free(buf); fclose(f); return NULL;
	}
	fclose(f);
	*out_len = (size_t)n;
	return buf;
}

static const char *nz(const char *s)
{
	return (s && s[0]) ? s : "-";
}

static void kv(const char *k, const char *v)
{
	printf("  %-22s %s\n", k, nz(v));
}

static void kv_u(const char *k, uint64_t v)
{
	printf("  %-22s %" PRIu64 "\n", k, v);
}

static void kv_h(const char *k, uint64_t v)
{
	printf("  %-22s 0x%" PRIx64 "\n", k, v);
}

static void dump_metadata(const struct ols_metadata *m)
{
	printf("── METADATA ──\n");
	kv("Make",                 m->make);
	kv("Model",                m->model);
	kv("Type",                 m->type);
	kv("Year",                 m->year);
	kv("Output",               m->output);
	kv("Cylinders",            m->cylinders);
	kv("Country",              m->country);
	kv("Drivetrain",           m->drivetrain);
	kv("Memory",               m->memory);
	kv("Manufacturer",         m->manufacturer);
	kv("ECU Name",             m->ecu_name);
	kv("HW Number",            m->hw_number);
	kv("SW Number",            m->sw_number);
	kv("Production No",        m->production_no);
	kv("Engine Code",          m->engine_code);
	kv("Transmission",         m->transmission);
	kv("Last Write (FILETIME)", "");
	printf("    0x%016" PRIx64 "\n", m->last_write_time);
	kv("Original File Name",   m->original_file_name);
	kv("WinOLS Version",       m->winols_version_string);
	kv("Revision Tag",         m->revision_tag);
	kv("Base Address (hex)",   m->base_address_hex);
	if (m->build_number_set)     kv_u("Build Number",       m->build_number);
	if (m->checksum_set)         kv_h("Checksum",           m->checksum);
	if (m->flags_set)            kv_h("Flags",              m->flags);
	if (m->import_comment[0])    kv("Import Comment",       m->import_comment);
	if (m->post_comment_flag_set)kv_u("Post Comment Flag",  m->post_comment_flag);
	if (m->tag[0])               kv("Tag",                  m->tag);
	if (m->region_count_set)     kv_u("Region Count",       m->region_count);
	if (m->notes[0])             kv("Notes",                m->notes);
	printf("\n");
}

static void dump_axis(const struct ols_axis *a, const char *label)
{
	if (!a->has_pts_address && !a->has_scaling && a->pts_count <= 0)
		return;
	printf("    %s axis: ", label);
	if (a->has_pts_address)
		printf("addr=0x%08x ", a->pts_address);
	if (a->has_scaling)
		printf("scale=%g offset=%g ", a->scale, a->offset);
	printf("dt=%u %db %s %s pts=%d",
	       a->pts_data_type, a->pts_data_size,
	       a->pts_signed ? "signed" : "unsigned",
	       a->pts_big_endian ? "BE" : "LE",
	       a->pts_count);
	if (a->input_name[0])
		printf(" input=%s", a->input_name);
	if (a->unit[0])
		printf(" [%s]", a->unit);
	printf("\n");
	if (a->npts_values > 0 && a->npts_values <= 16) {
		printf("      values: ");
		for (size_t i = 0; i < a->npts_values; i++)
			printf("%s%g", i ? ", " : "", a->pts_values[i]);
		printf("\n");
	} else if (a->npts_values > 16) {
		printf("      values: ");
		for (size_t i = 0; i < 8; i++)
			printf("%s%g", i ? ", " : "", a->pts_values[i]);
		printf(" ... (%zu total)\n", a->npts_values);
	}
}

static void dump_versions(const struct ols_ctx *c)
{
	printf("── VERSIONS (%zu) ──\n", c->nversions);
	for (size_t i = 0; i < c->nversions; i++) {
		const struct ols_version *v = &c->versions[i];
		printf("  [%d] %-16s  rom=%zu bytes  base=0x%08x  segs=%zu\n",
		       v->index, v->name, v->rom_size,
		       v->base_address, v->nsegments);
		for (size_t j = 0; j < v->nsegments; j++) {
			const struct ols_segment *s = &v->segments[j];
			printf("      seg[%zu] flash=0x%08x+0x%08x file=0x%zx  hash=0x%08x  framing=%u  %s",
			       j, s->flash_base, s->flash_size,
			       s->data_start, s->hash, s->framing_bytes,
			       s->is_primary ? "primary" : "nested");
			if (s->proj_tag[0])
				printf("  tag='%s'", s->proj_tag);
			printf("\n");
		}
	}
	printf("\n");
}

static void dump_maps(const struct ols_ctx *c, int verbose)
{
	printf("── MAPS (%zu) ──\n", c->nmaps);
	for (size_t i = 0; i < c->nmaps; i++) {
		const struct ols_map *m = &c->maps[i];
		printf("[%5zu] %-5s %3dx%-3d @%08x  %s\n",
		       i, m->type, m->nx, m->ny, m->raw_address, m->name);
		if (!verbose)
			continue;
		if (m->description[0] && strcmp(m->description, m->name) != 0)
			printf("    desc    : %s\n", m->description);
		printf("    cell    : %d byte%s %s %s  dt=%u  %s\n",
		       m->data_size, m->data_size == 1 ? "" : "s",
		       m->data_signed ? "signed" : "unsigned",
		       m->cell_big_endian ? "BE" : "LE",
		       m->cell_data_type,
		       m->column_major ? "col-major" : "row-major");
		if (m->has_scaling)
			printf("    scale   : y = %g * raw + %g%s%s\n",
			       m->scale, m->offset,
			       m->unit[0] ? "  [" : "",
			       m->unit[0] ? m->unit : "");
		if (m->rom_end > m->raw_address)
			printf("    extent  : 0x%08x..0x%08x (%u bytes)\n",
			       m->raw_address, m->rom_end,
			       m->rom_end - m->raw_address);
		dump_axis(&m->x_axis, "X");
		if (m->has_y_axis)
			dump_axis(&m->y_axis, "Y");
	}
	printf("\n");
}

static void json_indent(int n)
{
	for (int i = 0; i < n; i++)
		putchar(' ');
}

static void json_string(const char *s)
{
	putchar('"');
	for (; s && *s; s++) {
		unsigned char c = (unsigned char)*s;
		switch (c) {
		case '"':  printf("\\\""); break;
		case '\\': printf("\\\\"); break;
		case '\b': printf("\\b");  break;
		case '\f': printf("\\f");  break;
		case '\n': printf("\\n");  break;
		case '\r': printf("\\r");  break;
		case '\t': printf("\\t");  break;
		default:
			if (c < 0x20 || c >= 0x80)
				printf("\\u%04x", c);
			else
				putchar(c);
		}
	}
	putchar('"');
}

static void json_key(int indent, const char *key)
{
	json_indent(indent);
	json_string(key);
	printf(": ");
}

static void json_str_field(int indent, const char *key, const char *value,
			   int comma)
{
	json_key(indent, key);
	json_string(value);
	printf("%s\n", comma ? "," : "");
}

static void json_uint_field(int indent, const char *key, uint64_t value,
			    int comma)
{
	json_key(indent, key);
	printf("%" PRIu64 "%s\n", value, comma ? "," : "");
}

static void json_bool_field(int indent, const char *key, int value, int comma)
{
	json_key(indent, key);
	printf("%s%s\n", value ? "true" : "false", comma ? "," : "");
}

static void json_double_field(int indent, const char *key, double value,
			      int comma)
{
	json_key(indent, key);
	if (isfinite(value))
		printf("%.17g", value);
	else
		printf("null");
	printf("%s\n", comma ? "," : "");
}

static void json_hex_field(int indent, const char *key, uint64_t value,
			   int comma)
{
	json_key(indent, key);
	printf("\"0x%08" PRIx64 "\"%s\n", value, comma ? "," : "");
}

static void json_u32_array(int indent, const char *key,
			   const uint32_t *values, size_t count, int comma)
{
	json_key(indent, key);
	printf("[");
	for (size_t i = 0; i < count; i++)
		printf("%s%" PRIu32, i ? ", " : "", values[i]);
	printf("]%s\n", comma ? "," : "");
}

static void json_u8_array(int indent, const char *key,
			  const uint8_t *values, size_t count, int comma)
{
	json_key(indent, key);
	printf("[");
	for (size_t i = 0; i < count; i++)
		printf("%s%u", i ? ", " : "", (unsigned)values[i]);
	printf("]%s\n", comma ? "," : "");
}

static void json_bytes_hex(int indent, const char *key,
			   const uint8_t *bytes, size_t count, int comma)
{
	json_key(indent, key);
	putchar('"');
	for (size_t i = 0; i < count; i++)
		printf("%02x", bytes[i]);
	printf("\"%s\n", comma ? "," : "");
}

static void json_source_hex(int indent, const char *key,
			    const uint8_t *source, size_t file_size,
			    size_t start, size_t end, int comma)
{
	if (start < end && end <= file_size)
		json_bytes_hex(indent, key, source + start, end - start, comma);
	else {
		json_key(indent, key);
		printf("null%s\n", comma ? "," : "");
	}
}

static void dump_json_axis(const struct ols_axis *a, int indent,
			   const uint8_t *source, size_t file_size)
{
	json_indent(indent);
	printf("{\n");
	json_uint_field(indent + 2, "source_axis_offset", a->source_axis_offset, 1);
	json_uint_field(indent + 2, "source_axis_end", a->source_axis_end, 1);
	json_source_hex(indent + 2, "source_axis_hex", source, file_size,
			a->source_axis_offset, a->source_axis_end, 1);
	json_uint_field(indent + 2, "axis_type", a->axis_type, 1);
	json_uint_field(indent + 2, "raw_pts_address", a->raw_pts_address, 1);
	json_uint_field(indent + 2, "axis_rom_v2", a->axis_rom_v2, 1);
	json_uint_field(indent + 2, "raw_u120", a->raw_u120, 1);
	json_uint_field(indent + 2, "cell_bits", a->cell_bits, 1);
	json_u8_array(indent + 2, "raw_flags", a->raw_flags, 2, 1);
	json_uint_field(indent + 2, "raw_signed_flag", a->raw_signed_flag, 1);
	json_uint_field(indent + 2, "raw_schema8", a->raw_schema8, 1);
	json_uint_field(indent + 2, "raw_schema77", a->raw_schema77, 1);
	json_uint_field(indent + 2, "inline_count", a->inline_count, 1);
	json_str_field(indent + 2, "input_name", a->input_name, 1);
	json_str_field(indent + 2, "unit", a->unit, 1);
	json_uint_field(indent + 2, "pts_address", a->pts_address, 1);
	json_hex_field(indent + 2, "pts_address_hex", a->pts_address, 1);
	json_uint_field(indent + 2, "pts_data_type", a->pts_data_type, 1);
	json_uint_field(indent + 2, "pts_data_size", (uint64_t)a->pts_data_size, 1);
	json_bool_field(indent + 2, "pts_signed", a->pts_signed, 1);
	json_bool_field(indent + 2, "pts_big_endian", a->pts_big_endian, 1);
	json_bool_field(indent + 2, "has_pts_address", a->has_pts_address, 1);
	json_bool_field(indent + 2, "has_scaling", a->has_scaling, 1);
	json_double_field(indent + 2, "scale", a->scale, 1);
	json_double_field(indent + 2, "offset", a->offset, 1);
	json_uint_field(indent + 2, "pts_count", (uint64_t)a->pts_count, 1);
	json_key(indent + 2, "pts_values");
	printf("[");
	for (size_t i = 0; i < a->npts_values; i++) {
		if (i)
			printf(", ");
		if (isfinite(a->pts_values[i]))
			printf("%.17g", a->pts_values[i]);
		else
			printf("null");
	}
	printf("]\n");
	json_indent(indent);
	printf("}");
}

static void dump_json_metadata(const struct ols_metadata *m, int indent)
{
	json_indent(indent);
	printf("{\n");
	json_str_field(indent + 2, "make", m->make, 1);
	json_str_field(indent + 2, "model", m->model, 1);
	json_str_field(indent + 2, "type", m->type, 1);
	json_str_field(indent + 2, "year", m->year, 1);
	json_str_field(indent + 2, "output", m->output, 1);
	json_str_field(indent + 2, "cylinders", m->cylinders, 1);
	json_str_field(indent + 2, "country", m->country, 1);
	json_str_field(indent + 2, "drivetrain", m->drivetrain, 1);
	json_str_field(indent + 2, "memory", m->memory, 1);
	json_str_field(indent + 2, "manufacturer", m->manufacturer, 1);
	json_str_field(indent + 2, "ecu_name", m->ecu_name, 1);
	json_str_field(indent + 2, "hw_number", m->hw_number, 1);
	json_str_field(indent + 2, "sw_number", m->sw_number, 1);
	json_str_field(indent + 2, "production_no", m->production_no, 1);
	json_str_field(indent + 2, "engine_code", m->engine_code, 1);
	json_str_field(indent + 2, "transmission", m->transmission, 1);
	json_uint_field(indent + 2, "last_write_time", m->last_write_time, 1);
	json_str_field(indent + 2, "original_file_name", m->original_file_name, 1);
	json_str_field(indent + 2, "winols_version_string",
		       m->winols_version_string, 1);
	json_str_field(indent + 2, "revision_tag", m->revision_tag, 1);
	json_str_field(indent + 2, "base_address_hex", m->base_address_hex, 1);
	json_uint_field(indent + 2, "build_number", m->build_number, 1);
	json_bool_field(indent + 2, "build_number_set", m->build_number_set, 1);
	json_uint_field(indent + 2, "checksum", m->checksum, 1);
	json_bool_field(indent + 2, "checksum_set", m->checksum_set, 1);
	json_uint_field(indent + 2, "flags", m->flags, 1);
	json_bool_field(indent + 2, "flags_set", m->flags_set, 1);
	json_str_field(indent + 2, "import_comment", m->import_comment, 1);
	json_uint_field(indent + 2, "post_comment_flag",
			m->post_comment_flag, 1);
	json_bool_field(indent + 2, "post_comment_flag_set",
			m->post_comment_flag_set, 1);
	json_str_field(indent + 2, "tag", m->tag, 1);
	json_uint_field(indent + 2, "region_count", m->region_count, 1);
	json_bool_field(indent + 2, "region_count_set", m->region_count_set, 1);
	json_str_field(indent + 2, "notes", m->notes, 0);
	json_indent(indent);
	printf("}");
}

static void dump_json(const struct ols_ctx *c, const uint8_t *source,
		      size_t file_size)
{
	printf("{\n");
	json_key(2, "header");
	printf("{\n");
	json_uint_field(4, "file_size", file_size, 1);
	json_uint_field(4, "magic", c->header.magic, 1);
	json_hex_field(4, "magic_hex", c->header.magic, 1);
	json_uint_field(4, "format_version", c->header.format_version, 1);
	json_uint_field(4, "declared_size", c->header.declared_size, 1);
	json_bool_field(4, "valid", c->header.valid, 0);
	printf("  },\n");

	json_key(2, "anchors");
	printf("[");
	for (size_t i = 0; i < 7; i++)
		printf("%s%" PRId64, i ? ", " : "", c->anchors[i]);
	printf("],\n");

	json_key(2, "metadata");
	dump_json_metadata(&c->metadata, 2);
	printf(",\n");

	json_key(2, "versions");
	printf("[\n");
	for (size_t i = 0; i < c->nversions; i++) {
		const struct ols_version *v = &c->versions[i];
		json_indent(4);
		printf("{\n");
		json_uint_field(6, "index", (uint64_t)v->index, 1);
		json_str_field(6, "name", v->name, 1);
		json_uint_field(6, "base_address", v->base_address, 1);
		json_hex_field(6, "base_address_hex", v->base_address, 1);
		json_uint_field(6, "rom_size", v->rom_size, 1);
		json_uint_field(6, "segments", v->nsegments, 0);
		json_indent(4);
		printf("}%s\n", i + 1 < c->nversions ? "," : "");
	}
	printf("  ],\n");

	json_key(2, "maps");
	printf("[\n");
	for (size_t i = 0; i < c->nmaps; i++) {
		const struct ols_map *m = &c->maps[i];
		json_indent(4);
		printf("{\n");
		json_uint_field(6, "index", i, 1);
		json_uint_field(6, "source_record_offset", m->source_record_offset, 1);
		json_uint_field(6, "source_record_end", m->source_record_end, 1);
		json_source_hex(6, "source_record_hex", source, file_size,
				m->source_record_offset, m->source_record_end, 1);
		json_uint_field(6, "kennfeld_type", m->kennfeld_type, 1);
		json_u32_array(6, "raw_header_fields", m->raw_header_fields, 6, 1);
		json_uint_field(6, "raw_u94", m->raw_u94, 1);
		json_uint_field(6, "raw_flag74", m->raw_flag74, 1);
		json_bytes_hex(6, "raw_display_hex", m->raw_display_bytes, 48, 1);
		json_u8_array(6, "raw_flags", m->raw_flags, 4, 1);
		json_u32_array(6, "raw_geometry", m->raw_geometry, 5, 1);
		json_uint_field(6, "rom_base", m->rom_base, 1);
		json_uint_field(6, "raw_u61", m->raw_u61, 1);
		json_u32_array(6, "raw_u105", m->raw_u105, 4, 1);
		json_str_field(6, "name", m->name, 1);
		json_str_field(6, "description", m->description, 1);
		json_str_field(6, "type", m->type, 1);
		json_str_field(6, "unit", m->unit, 1);
		json_uint_field(6, "raw_address", m->raw_address, 1);
		json_hex_field(6, "raw_address_hex", m->raw_address, 1);
		json_uint_field(6, "address", m->address, 1);
		json_hex_field(6, "address_hex", m->address, 1);
		json_uint_field(6, "rom_end", m->rom_end, 1);
		json_hex_field(6, "rom_end_hex", m->rom_end, 1);
		json_uint_field(6, "nx", (uint64_t)m->nx, 1);
		json_uint_field(6, "ny", (uint64_t)m->ny, 1);
		json_uint_field(6, "data_size", (uint64_t)m->data_size, 1);
		json_bool_field(6, "data_signed", m->data_signed, 1);
		json_bool_field(6, "cell_big_endian", m->cell_big_endian, 1);
		json_uint_field(6, "cell_data_type", m->cell_data_type, 1);
		json_bool_field(6, "column_major", m->column_major, 1);
		json_bool_field(6, "column_major_is_assumed", m->column_major_is_assumed, 1);
		json_bool_field(6, "has_scaling", m->has_scaling, 1);
		json_double_field(6, "scale", m->scale, 1);
		json_double_field(6, "offset", m->offset, 1);
		json_key(6, "x_axis");
		dump_json_axis(&m->x_axis, 6, source, file_size);
		printf(",\n");
		json_bool_field(6, "has_y_axis", m->has_y_axis, 1);
		json_key(6, "y_axis");
		dump_json_axis(&m->y_axis, 6, source, file_size);
		printf(",\n");
		json_key(6, "raw_rows_axis");
		dump_json_axis(&m->raw_rows_axis, 6, source, file_size);
		printf(",\n");
		json_key(6, "raw_cols_axis");
		dump_json_axis(&m->raw_cols_axis, 6, source, file_size);
		printf("\n");
		json_indent(4);
		printf("}%s\n", i + 1 < c->nmaps ? "," : "");
	}
	printf("  ],\n");

	json_key(2, "warnings");
	printf("[");
	for (size_t i = 0; i < c->nwarnings; i++) {
		if (i)
			printf(", ");
		json_string(c->warnings[i]);
	}
	printf("]\n");
	printf("}\n");
}

static int usage(const char *a)
{
	fprintf(stderr, "usage: %s [-v] [-q] [-j|--json] <file.ols>\n"
	                "  -v  dump full per-map metadata (cell, scaling, axes)\n"
	                "  -q  omit per-map listing (header + metadata + versions only)\n"
	                "  -j  write machine-readable JSON\n", a);
	return 2;
}

int main(int argc, char **argv)
{
	struct ols_ctx ctx;
	size_t len;
	void *data;
	const char *path = NULL;
	int verbose = 0, quiet = 0, json = 0;
	char slurp_err[256];

	for (int i = 1; i < argc; i++) {
		const char *s = argv[i];
		if (!strcmp(s, "--json")) {
			json = 1;
		} else if (!strcmp(s, "--verbose")) {
			verbose = 1;
		} else if (!strcmp(s, "--quiet")) {
			quiet = 1;
		} else if (s[0] == '-' && s[1]) {
			for (int j = 1; s[j]; j++) {
				switch (s[j]) {
				case 'v': verbose = 1; break;
				case 'q': quiet = 1; break;
				case 'j': json = 1; break;
				default:  return usage(argv[0]);
				}
			}
		} else {
			if (path)
				return usage(argv[0]);
			path = s;
		}
	}
	if (!path)
		return usage(argv[0]);

	slurp_err[0] = 0;
	data = slurp(path, &len, slurp_err, sizeof(slurp_err));
	if (!data) {
		fprintf(stderr, "%s\n", slurp_err[0] ? slurp_err : "failed to read file");
		return 1;
	}

	if (ols_parse(data, len, &ctx) < 0) {
		fprintf(stderr, "ols_parse: %s\n", ctx.err);
		ols_free(&ctx);
		free(data);
		return 1;
	}

	if (json) {
		dump_json(&ctx, data, len);
		ols_free(&ctx);
		free(data);
		return 0;
	}

	printf("── HEADER ──\n");
	printf("  file_size       %zu bytes\n", len);
	printf("  magic           0x%08x\n", ctx.header.magic);
	printf("  format_version  %u\n", ctx.header.format_version);
	printf("  declared_size   %u\n", ctx.header.declared_size);
	printf("  anchors         M1=%lld M2=%lld M3=%lld M4=%lld M5=%lld M6=%lld M7=%lld\n",
	       (long long)ctx.anchors[0], (long long)ctx.anchors[1],
	       (long long)ctx.anchors[2], (long long)ctx.anchors[3],
	       (long long)ctx.anchors[4], (long long)ctx.anchors[5],
	       (long long)ctx.anchors[6]);
	printf("\n");

	dump_metadata(&ctx.metadata);
	dump_versions(&ctx);
	if (!quiet)
		dump_maps(&ctx, verbose);

	if (ctx.nwarnings) {
		printf("── WARNINGS (%zu) ──\n", ctx.nwarnings);
		for (size_t i = 0; i < ctx.nwarnings; i++)
			printf("  %s\n", ctx.warnings[i]);
	}

	ols_free(&ctx);
	free(data);
	return 0;
}

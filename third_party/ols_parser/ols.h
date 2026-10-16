#ifndef OLS_H
#define OLS_H

#include <stddef.h>
#include <stdint.h>

struct ols_header {
	uint32_t magic;
	uint32_t format_version;
	uint32_t declared_size;
	int      valid;
};

struct ols_metadata {
	char     make[128];
	char     model[128];
	char     type[64];
	char     year[16];
	char     output[32];
	char     cylinders[16];
	char     country[64];
	char     drivetrain[32];
	char     memory[32];
	char     manufacturer[128];
	char     ecu_name[128];
	char     hw_number[64];
	char     sw_number[64];
	char     production_no[64];
	char     engine_code[64];
	char     transmission[64];
	uint64_t last_write_time;
	char     original_file_name[256];
	char     winols_version_string[64];
	char     reserved2[64];
	char     revision_tag[64];
	char     reserved3[64];
	char     reserved4[64];
	char     reserved5[64];
	char     base_address_hex[32];
	uint32_t build_number;
	uint64_t checksum;
	uint32_t flags;
	char     import_comment[512];
	uint32_t post_comment_flag;
	char     tag[64];
	uint32_t region_count;
	char     notes[1024];
	int      build_number_set;
	int      checksum_set;
	int      flags_set;
	int      post_comment_flag_set;
	int      region_count_set;
};

struct ols_segment {
	int      segment_index;
	int      is_primary;
	uint32_t flash_base;
	uint32_t flash_size;
	uint32_t hash;
	uint32_t framing_bytes;
	size_t   data_start;
	char     proj_tag[16];
};

struct ols_axis {
	size_t   source_axis_offset;
	size_t   source_axis_end;
	uint32_t axis_type;
	uint32_t raw_pts_address;
	uint32_t axis_rom_v2;
	uint32_t raw_u120;
	uint32_t cell_bits;
	uint8_t  raw_flags[2];
	uint8_t  raw_signed_flag;
	uint32_t raw_schema8;
	uint32_t raw_schema77;
	uint32_t inline_count;
	char     input_name[128];
	char     unit[32];
	uint32_t pts_address;
	uint32_t pts_data_type;
	int      pts_data_size;
	int      pts_signed;
	int      pts_big_endian;
	int      has_pts_address;
	int      has_scaling;
	double   scale;
	double   offset;
	int      pts_count;
	double  *pts_values;
	size_t   npts_values;
};

struct ols_map {
	size_t          source_record_offset;
	size_t          source_record_end;
	uint32_t        kennfeld_type;
	uint32_t        raw_header_fields[6];
	uint32_t        raw_u94;
	uint8_t         raw_flag74;
	uint8_t         raw_display_bytes[48];
	uint8_t         raw_flags[4];
	uint32_t        raw_geometry[5];
	uint32_t        rom_base;
	uint32_t        raw_u61;
	uint32_t        raw_u105[4];
	int             column_major_is_assumed;
	char            name[128];
	char            description[256];
	char            unit[32];
	const char     *type;
	uint32_t        raw_address;
	uint32_t        address;
	uint32_t        rom_end;
	int             nx;
	int             ny;
	int             data_size;
	int             data_signed;
	int             cell_big_endian;
	uint32_t        cell_data_type;
	int             column_major;
	int             has_scaling;
	double          scale;
	double          offset;
	struct ols_axis x_axis;
	struct ols_axis y_axis;
	struct ols_axis raw_rows_axis;
	struct ols_axis raw_cols_axis;
	int             has_y_axis;
};

struct ols_version {
	int                 index;
	char                name[64];
	uint32_t            base_address;
	uint8_t            *rom_data;
	size_t              rom_size;
	struct ols_segment *segments;
	size_t              nsegments;
};

struct ols_ctx {
	struct ols_header     header;
	struct ols_metadata   metadata;
	struct ols_version   *versions;
	size_t                nversions;
	struct ols_map       *maps;
	size_t                nmaps;
	int64_t               anchors[7];
	char                  err[256];
	char                **warnings;
	size_t                nwarnings;
};

int  ols_parse(const void *data, size_t len, struct ols_ctx *ctx);
void ols_free(struct ols_ctx *ctx);

#endif

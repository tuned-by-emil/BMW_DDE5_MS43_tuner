#include "ols.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define OLS_MAGIC_VAL   0x0000000Bu
#define OLS_SIG         "WinOLS File"
#define OLS_SIG_LEN     11
#define OLS_HDR_SIZE    24

#define M1 0x42007899u
#define M2 0x11883377u
#define M3 0x98728833u
#define M4 0x08260064u
#define M5 0xCD23018Au
#define M6 0x88271283u
#define M7 0x84C0AD36u

#define SENTINEL_A      0xFADECAFEu
#define SENTINEL_B      0xCAFEAFFEu
#define POSTZIP_HDR     0x98638811u

#define DESC_PRE_OFF    20
#define PROJ_SLOT_LEN   18
#define PROJ_TAG_LEN    10
#define DATA_START_PRE  26
#define FRAMING_BYTES   76u
#define MAX_SEG_SIZE    (16u * 1024u * 1024u)
#define MODERN_FMT_MIN  200u

#define DIM_A276_MAX    0x4000u
#define DIM_A396_MAX    0x400u
#define DIM_A400_MAX    0x1F400u

static inline uint16_t rd16(const uint8_t *p)
{
	return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static inline uint32_t rd32(const uint8_t *p)
{
	return (uint32_t)rd16(p) | ((uint32_t)rd16(p + 2) << 16);
}

static inline uint64_t rd64(const uint8_t *p)
{
	return (uint64_t)rd32(p) | ((uint64_t)rd32(p + 4) << 32);
}

static inline double rdf64(const uint8_t *p)
{
	uint64_t u = rd64(p);
	double f;
	memcpy(&f, &u, 8);
	return f;
}

static int fail(struct ols_ctx *c, const char *msg)
{
	snprintf(c->err, sizeof(c->err), "%s", msg);
	return -1;
}

static char *xstrdup(const char *s)
{
	size_t n = strlen(s) + 1;
	char *p = malloc(n);
	if (p)
		memcpy(p, s, n);
	return p;
}

static void warn(struct ols_ctx *c, const char *fmt, ...)
{
	char buf[256];
	va_list ap;
	char **nw;

	va_start(ap, fmt);
	vsnprintf(buf, sizeof(buf), fmt, ap);
	va_end(ap);

	nw = realloc(c->warnings, (c->nwarnings + 1) * sizeof(*nw));
	if (!nw)
		return;
	c->warnings = nw;
	c->warnings[c->nwarnings] = xstrdup(buf);
	if (c->warnings[c->nwarnings])
		c->nwarnings++;
}

struct cur {
	const uint8_t *buf;
	size_t         len;
	size_t         off;
	uint32_t       schema;
};

static int cur_need(struct cur *c, size_t n)
{
	return c->off + n <= c->len;
}

static uint8_t cur_u8(struct cur *c)
{
	if (!cur_need(c, 1))
		return 0;
	return c->buf[c->off++];
}

static uint32_t cur_u32(struct cur *c)
{
	uint32_t v;
	if (!cur_need(c, 4))
		return 0;
	v = rd32(c->buf + c->off);
	c->off += 4;
	return v;
}

static uint64_t cur_u64(struct cur *c)
{
	uint64_t v;
	if (!cur_need(c, 8))
		return 0;
	v = rd64(c->buf + c->off);
	c->off += 8;
	return v;
}

static double cur_f64(struct cur *c)
{
	double v;
	if (!cur_need(c, 8))
		return 0;
	v = rdf64(c->buf + c->off);
	c->off += 8;
	return v;
}

static int is_text(const uint8_t *p, size_t n)
{
	size_t ok = 0;
	if (!n)
		return 0;
	for (size_t i = 0; i < n; i++) {
		uint8_t b = p[i];
		if ((b >= 32 && b < 127) || b == 9 || b == 10 || b == 13 ||
		    b >= 0x80)
			ok++;
	}
	return ok * 100 >= n * 85;
}

static int is_ident(const char *s, size_t n)
{
	if (!n || n > 40)
		return 0;
	uint8_t c = (uint8_t)s[0];
	if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '_'))
		return 0;
	for (size_t i = 1; i < n; i++) {
		c = (uint8_t)s[i];
		if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
		      (c >= '0' && c <= '9') || c == '_'))
			return 0;
	}
	return 1;
}

struct pstr {
	size_t   end;
	int      valid;
	char     text[512];
};

static struct pstr read_cstring(const uint8_t *buf, size_t len,
				size_t off, uint32_t schema, size_t max_len)
{
	struct pstr r = { 0 };
	int32_t slen;
	size_t need;

	if (off + 4 > len)
		return r;

	slen = (int32_t)rd32(buf + off);

	if (schema >= 439) {
		if (slen == -1) { strcpy(r.text, "-"); r.end = off + 4; r.valid = 1; return r; }
		if (slen == -2) { strcpy(r.text, "?"); r.end = off + 4; r.valid = 1; return r; }
		if (slen == -3) { strcpy(r.text, "%"); r.end = off + 4; r.valid = 1; return r; }
		if (slen <= 0)  { r.end = off + 4; r.valid = 1; return r; }
		if ((size_t)slen > max_len || off + 4 + (size_t)slen > len)
			return r;
		if (!is_text(buf + off + 4, (size_t)slen))
			return r;
		size_t cap = sizeof(r.text) - 1;
		size_t n = (size_t)slen < cap ? (size_t)slen : cap;
		memcpy(r.text, buf + off + 4, n);
		r.text[n] = 0;
		r.end = off + 4 + (size_t)slen;
		r.valid = 1;
		return r;
	}

	if (slen < 0 || (size_t)slen > max_len)
		return r;
	if (slen == 0) {
		r.end = off + 4;
		r.valid = 1;
		return r;
	}

	need = 4 + (size_t)slen + 1;
	if (off + need > len)
		return r;
	if (!is_text(buf + off + 4, (size_t)slen))
		return r;
	if (buf[off + 4 + (size_t)slen] != 0x00)
		return r;

	size_t cap = sizeof(r.text) - 1;
	size_t n = (size_t)slen < cap ? (size_t)slen : cap;
	memcpy(r.text, buf + off + 4, n);
	r.text[n] = 0;
	r.end = off + need;
	r.valid = 1;
	return r;
}

static struct pstr cur_cstr(struct cur *c, size_t max_len)
{
	struct pstr r = read_cstring(c->buf, c->len, c->off, c->schema, max_len);
	if (r.valid)
		c->off = r.end;
	return r;
}

struct cfr {
	size_t end;
	int    valid;
	char   name[512];
};

static struct cfr read_cfolderref(const uint8_t *buf, size_t len,
				  size_t off, uint32_t schema)
{
	struct cfr r = { 0 };
	struct pstr inner = read_cstring(buf, len, off, schema, 8192);
	size_t o;

	if (!inner.valid)
		return r;

	memcpy(r.name, inner.text, sizeof(r.name) - 1);
	o = inner.end;

	if (schema < 330) {
		r.end = o;
		r.valid = 1;
		return r;
	}
	if (schema >= 345) {
		if (o + 4 > len) return r;
		o += 4;
		if (o + 4 > len) return r;
		o += 4;
	}
	if (o + 4 > len) return r;
	uint32_t count = rd32(buf + o);
	o += 4;
	if (count > 0x1000)
		return r;
	if (schema >= 345 && count > 0) {
		for (uint32_t i = 0; i < count; i++) {
			struct pstr e = read_cstring(buf, len, o, schema, 8192);
			if (!e.valid)
				return r;
			o = e.end;
			if (o + 4 > len)
				return r;
			o += 4;
		}
	}
	r.end = o;
	r.valid = 1;
	return r;
}

static struct cfr cur_cfr(struct cur *c)
{
	struct cfr r = read_cfolderref(c->buf, c->len, c->off, c->schema);
	if (r.valid)
		c->off = r.end;
	return r;
}

static int header_parse(const uint8_t *buf, size_t len,
			struct ols_header *h, struct ols_ctx *ctx)
{
	memset(h, 0, sizeof(*h));
	if (len < OLS_HDR_SIZE)
		return fail(ctx, "file too small for OLS header");

	h->magic = rd32(buf);
	if (h->magic != OLS_MAGIC_VAL)
		return fail(ctx, "bad magic");
	if (memcmp(buf + 4, OLS_SIG, OLS_SIG_LEN) != 0)
		return fail(ctx, "signature mismatch");
	if (buf[0x0F] != 0x00)
		return fail(ctx, "missing NUL after signature");

	h->format_version = rd32(buf + 0x10);
	h->declared_size  = rd32(buf + 0x14);
	h->valid = 1;
	return 0;
}

static long find_u32_le(const uint8_t *buf, size_t len, uint32_t v, size_t from)
{
	uint8_t n[4] = {
		(uint8_t)(v & 0xFF),
		(uint8_t)((v >> 8) & 0xFF),
		(uint8_t)((v >> 16) & 0xFF),
		(uint8_t)((v >> 24) & 0xFF),
	};
	if (from >= len || len < 4)
		return -1;
	for (size_t i = from; i + 4 <= len; i++)
		if (memcmp(buf + i, n, 4) == 0)
			return (long)i;
	return -1;
}

static void scan_magic(const uint8_t *buf, size_t len, int64_t anchors[7])
{
	static const uint32_t m[7] = { M1, M2, M3, M4, M5, M6, M7 };
	for (int i = 0; i < 7; i++)
		anchors[i] = find_u32_le(buf, len, m[i], 0);
}

#define CPY_STR(dst, src) do { \
	size_t _cap = sizeof(dst) - 1; \
	size_t _n = strlen(src); \
	if (_n > _cap) _n = _cap; \
	memcpy(dst, src, _n); \
	dst[_n] = 0; \
} while (0)

static void parse_metadata(struct cur *c, struct ols_metadata *m,
			   struct ols_ctx *ctx)
{
	struct pstr r;

#define READ_STR(field, name) do { \
	r = cur_cstr(c, 8192); \
	if (!r.valid) { warn(ctx, "metadata: failed at '%s'", name); return; } \
	CPY_STR(m->field, r.text); \
} while (0)

	READ_STR(make, "Make");
	READ_STR(model, "Model");
	READ_STR(type, "Type");
	READ_STR(year, "Year");
	READ_STR(output, "Output");
	READ_STR(cylinders, "Cylinders");
	READ_STR(country, "Country");
	READ_STR(drivetrain, "Drivetrain");
	READ_STR(memory, "Memory");
	READ_STR(manufacturer, "Manufacturer");
	READ_STR(ecu_name, "EcuName");
	if (c->schema == 113) {
		/* This legacy schema has a reserved slot before HW and SW. */
		READ_STR(reserved3, "LegacyReserved11");
		READ_STR(hw_number, "HwNumber");
		READ_STR(sw_number, "SwNumber");
		READ_STR(engine_code, "EngineCode");
		READ_STR(reserved4, "LegacyProjectComment");
		CPY_STR(m->transmission, m->drivetrain);
	} else {
		READ_STR(hw_number, "HwNumber");
		READ_STR(sw_number, "SwNumber");
		READ_STR(production_no, "ProductionNo");
		READ_STR(engine_code, "EngineCode");
		READ_STR(transmission, "Transmission");
	}

	if (!cur_need(c, 8)) { warn(ctx, "metadata: failed at 'LastWriteTime'"); return; }
	m->last_write_time = cur_u64(c);

	READ_STR(original_file_name, "OriginalFileName");
	READ_STR(winols_version_string, "WinOLSVersionString");
	READ_STR(reserved2, "Reserved2");
	READ_STR(revision_tag, "RevisionTag");
	READ_STR(reserved3, "Reserved3");
	READ_STR(reserved4, "Reserved4");
	READ_STR(reserved5, "Reserved5");

	if (c->schema >= 0x6E) {
		r = cur_cstr(c, 8192);
		if (r.valid)
			CPY_STR(m->base_address_hex, r.text);
	}
	if (c->schema >= 0x30 && cur_need(c, 4)) {
		m->build_number = cur_u32(c);
		m->build_number_set = 1;
	}
	if (c->schema >= 0x34 && cur_need(c, 8)) {
		m->checksum = cur_u64(c);
		m->checksum_set = 1;
	}
	if (c->schema >= 0x38 && cur_need(c, 4)) {
		m->flags = cur_u32(c);
		m->flags_set = 1;
	}
	if (c->schema >= 0x3E) {
		r = cur_cstr(c, 8192);
		if (r.valid)
			CPY_STR(m->import_comment, r.text);
	}
	if (c->schema >= 0x3F && cur_need(c, 4)) {
		m->post_comment_flag = cur_u32(c);
		m->post_comment_flag_set = 1;
	}
	if (c->schema >= 0x40) {
		r = cur_cstr(c, 8192);
		if (r.valid)
			CPY_STR(m->tag, r.text);
	}
	if (c->schema >= 0x4F && cur_need(c, 4)) {
		m->region_count = cur_u32(c);
		m->region_count_set = 1;
	}
	if (c->schema >= 0x55) {
		r = cur_cstr(c, 8192);
		if (r.valid)
			CPY_STR(m->notes, r.text);
	}
#undef READ_STR
}

static int is_printable_byte(uint8_t b) { return b >= 0x20 && b < 0x7F; }

static int valid_proj_slot(const uint8_t *buf, size_t len, size_t off)
{
	if (off + PROJ_TAG_LEN > len)
		return 0;
	int all_af = 1, all_p = 1;
	for (int i = 0; i < PROJ_TAG_LEN; i++) {
		uint8_t b = buf[off + i];
		if (b != 0xAF) all_af = 0;
		if (!is_printable_byte(b)) all_p = 0;
	}
	return all_af || all_p;
}

static void decode_proj_tag(const uint8_t *buf, size_t len, size_t off,
			    char *dst, size_t cap)
{
	size_t j = 0;
	if (off + PROJ_TAG_LEN > len) { if (cap) dst[0] = 0; return; }
	for (int i = 0; i < PROJ_TAG_LEN; i++) {
		uint8_t b = buf[off + i];
		if (is_printable_byte(b) && b != 0xAF && j + 1 < cap)
			dst[j++] = (char)b;
	}
	dst[j] = 0;
}

static int parse_descriptor(const uint8_t *buf, size_t len, size_t fade_off,
			    struct ols_segment *out)
{
	size_t desc_off, proj_off;
	uint32_t d[8];
	uint32_t fb, fe, sz;

	if (fade_off < DESC_PRE_OFF)
		return 0;
	desc_off = fade_off - DESC_PRE_OFF;
	if (desc_off < PROJ_SLOT_LEN || desc_off + 32 > len)
		return 0;
	for (int i = 0; i < 8; i++)
		d[i] = rd32(buf + desc_off + i * 4);
	if (d[5] != SENTINEL_A || d[6] != SENTINEL_B)
		return 0;

	fb = d[3] & 0x7FFFFFFF;
	fe = d[4] & 0x7FFFFFFF;
	if (fb >= fe)
		return 0;
	sz = fe - fb + 1;
	if (sz > MAX_SEG_SIZE)
		return 0;

	proj_off = desc_off - PROJ_SLOT_LEN;
	if (!valid_proj_slot(buf, len, proj_off))
		return 0;

	out->flash_base    = fb;
	out->flash_size    = sz;
	out->hash          = d[1];
	out->data_start    = proj_off - DATA_START_PRE;
	out->framing_bytes = (sz > FRAMING_BYTES) ? FRAMING_BYTES : 0u;
	out->is_primary    = 1;
	decode_proj_tag(buf, len, proj_off, out->proj_tag, sizeof(out->proj_tag));
	return 1;
}

static struct ols_segment *find_segments(const uint8_t *buf, size_t len,
					 size_t *out_n)
{
	struct ols_segment *segs = NULL;
	size_t cap = 0, n = 0;
	long pos = 0;

	while (1) {
		long j = find_u32_le(buf, len, SENTINEL_A, (size_t)pos);
		if (j < 0)
			break;
		if ((size_t)j + 8 <= len &&
		    rd32(buf + j + 4) == SENTINEL_B) {
			if (n == cap) {
				cap = cap ? cap * 2 : 16;
				struct ols_segment *r = realloc(segs, cap * sizeof(*segs));
				if (!r) { free(segs); *out_n = 0; return NULL; }
				segs = r;
			}
			memset(&segs[n], 0, sizeof(segs[n]));
			if (parse_descriptor(buf, len, (size_t)j, &segs[n]))
				n++;
		}
		pos = j + 1;
	}
	*out_n = n;
	return segs;
}

static void classify_primaries(struct ols_segment *segs, size_t n)
{
	uint32_t prev_end = 0;
	int have_prev = 0;
	for (size_t i = 0; i < n; i++) {
		uint32_t ds = (uint32_t)segs[i].data_start;
		int primary = !have_prev || ds >= prev_end;
		segs[i].is_primary = primary;
		if (primary) {
			prev_end = ds + segs[i].flash_size;
			have_prev = 1;
		}
	}
}

static int old_format_version(const uint8_t *buf, size_t len,
			      struct ols_segment *seg)
{
	long m1 = find_u32_le(buf, len, M1, 0);
	if (m1 < 0 || m1 < 12 || m1 + 4 > (long)len)
		return 0;

	size_t hdr = (size_t)m1 - 12;
	uint32_t n_minus_1 = rd32(buf + hdr);
	uint32_t vstart    = rd32(buf + hdr + 4);
	uint32_t vrec      = rd32(buf + hdr + 8);

	uint32_t nver = n_minus_1 + 1;
	if (nver == 0 || nver > 64 || vrec == 0 || vrec > 64u * 1024 * 1024)
		return 0;

	size_t expected = (size_t)vstart + 4 + (size_t)vrec * nver;
	if (expected > len)
		return 0;

	size_t payload = (size_t)vstart + 4;
	memset(seg, 0, sizeof(*seg));
	seg->flash_base    = (uint32_t)payload;
	seg->flash_size    = vrec;
	seg->data_start    = payload;
	seg->framing_bytes = 0;
	seg->is_primary    = 1;
	return (int)nver;
}

struct seg_group {
	struct ols_segment *segs;
	size_t              n;
};

static struct seg_group *group_by_version(struct ols_segment *segs, size_t n,
					  size_t *out_groups)
{
	struct seg_group *g = NULL;
	size_t ng = 0, cap = 0;
	long prev = -1;
	size_t start = 0;

	for (size_t i = 0; i < n; i++) {
		if ((long)segs[i].flash_base < prev) {
			if (ng == cap) {
				cap = cap ? cap * 2 : 4;
				struct seg_group *r = realloc(g, cap * sizeof(*r));
				if (!r) { free(g); *out_groups = 0; return NULL; }
				g = r;
			}
			g[ng].segs = &segs[start];
			g[ng].n    = i - start;
			ng++;
			start = i;
		}
		prev = (long)segs[i].flash_base;
	}
	if (start < n) {
		if (ng == cap) {
			cap = cap ? cap * 2 : 4;
			struct seg_group *r = realloc(g, cap * sizeof(*r));
			if (!r) { free(g); *out_groups = 0; return NULL; }
			g = r;
		}
		g[ng].segs = &segs[start];
		g[ng].n    = n - start;
		ng++;
	}
	*out_groups = ng;
	return g;
}

static int assemble_rom(const uint8_t *buf, size_t len,
			const struct ols_segment *segs, size_t n,
			uint8_t **rom_out, size_t *rom_len,
			uint32_t *base_out)
{
	uint32_t min_base = 0xFFFFFFFFu, max_end = 0;
	uint8_t *rom;
	size_t rom_sz;

	for (size_t i = 0; i < n; i++) {
		if (segs[i].flash_size <= segs[i].framing_bytes)
			continue;
		uint32_t eb = segs[i].flash_base + segs[i].framing_bytes;
		uint32_t ee = segs[i].flash_base + segs[i].flash_size;
		if (eb < min_base) min_base = eb;
		if (ee > max_end)  max_end = ee;
	}
	if (max_end <= min_base) {
		*rom_out = NULL;
		*rom_len = 0;
		*base_out = 0;
		return 0;
	}

	rom_sz = max_end - min_base;
	rom = malloc(rom_sz);
	if (!rom)
		return -1;
	memset(rom, 0xFF, rom_sz);

	for (size_t i = 0; i < n; i++) {
		const struct ols_segment *s = &segs[i];
		if (s->flash_size <= s->framing_bytes)
			continue;
		uint32_t eb = s->flash_base + s->framing_bytes;
		uint32_t ss = s->flash_size - s->framing_bytes;
		size_t dst = eb - min_base;
		size_t src = s->data_start + s->framing_bytes;
		if (src + ss > len) {
			if (src >= len) continue;
			ss = (uint32_t)(len - src);
		}
		if (dst + ss > rom_sz) ss = (uint32_t)(rom_sz - dst);
		memcpy(rom + dst, buf + src, ss);
	}

	*rom_out = rom;
	*rom_len = rom_sz;
	*base_out = min_base;
	return 0;
}

static int bytes_from_dtype(uint32_t dt)
{
	switch (dt) {
	case 2: case 3:                     return 2;
	case 4: case 5: case 6: case 7:     return 4;
	case 10: case 11: case 12: case 13: return 8;
	}
	return 1;
}

static int is_be_dtype(uint32_t dt)
{
	return dt == 2 || dt == 4 || dt == 6 || dt == 10 || dt == 12;
}

static int cb_to_bytes(uint32_t cb)
{
	switch (cb) {
	case 2:  return 1;
	case 8:  return 1;
	case 10: return 2;
	case 16: return 2;
	case 32: return 4;
	}
	return 2;
}

struct axis_res {
	size_t end;
	int    valid;
	struct ols_axis ax;
};

static struct axis_res read_axis(const uint8_t *buf, size_t len, size_t start,
				 uint32_t schema, int expected_sz)
{
	struct axis_res r = { 0 };
	size_t o = start;
	struct cfr cfr_;
	struct pstr u;
	double scale, offset;
	uint32_t axis_type, axis_rom, axis_dt, u120, cell_bits;
	uint32_t inline_count, axis_rom_v2 = 0;
	int flag3 = 0;
	r.ax.source_axis_offset = start;

	if (o + 4 > len)
		return r;

	cfr_ = read_cfolderref(buf, len, o, schema);
	if (!cfr_.valid)
		return r;
	CPY_STR(r.ax.input_name, cfr_.name);
	o = cfr_.end;

	u = read_cstring(buf, len, o, schema, 8192);
	if (!u.valid)
		return r;
	CPY_STR(r.ax.unit, u.text);
	o = u.end;

	if (o + 8 > len) return r;
	scale = rdf64(buf + o); o += 8;
	if (o + 8 > len) return r;
	offset = rdf64(buf + o); o += 8;

	if (o + 20 > len) return r;
	axis_type = rd32(buf + o); o += 4;
	axis_rom  = rd32(buf + o); o += 4;
	axis_dt   = rd32(buf + o); o += 4;
	u120      = rd32(buf + o); o += 4;
	cell_bits = rd32(buf + o); o += 4;
	r.ax.axis_type = axis_type;
	r.ax.raw_pts_address = axis_rom;
	r.ax.raw_u120 = u120;
	r.ax.cell_bits = cell_bits;

	if (cell_bits != 2 && cell_bits != 10 && cell_bits != 16)
		cell_bits = 10;

	if (o + 2 > len) return r;
	r.ax.raw_flags[0] = buf[o];
	r.ax.raw_flags[1] = buf[o + 1];
	o += 2;

	if (schema >= 264)      { if (o + 8 > len) return r; o += 8; }
	if (schema >= 241)      { if (o + 4 > len) return r; o += 4; }
	if (schema >= 8)        { if (o + 4 > len) return r; r.ax.raw_schema8 = rd32(buf + o); o += 4; }
	if (schema >= 12)       {
		if (o + 1 > len) return r;
		r.ax.raw_signed_flag = buf[o];
		flag3 = buf[o] != 0;
		o += 1;
	}
	if (schema >= 73) {
		if (o + 4 > len) return r;
		inline_count = rd32(buf + o); o += 4;
		r.ax.inline_count = inline_count;
		if (inline_count > 0x10000 || o + inline_count > len)
			return r;
		o += inline_count;
	}
	if (schema >= 77)       { if (o + 4 > len) return r; r.ax.raw_schema77 = rd32(buf + o); o += 4; }
	if (schema >= 91)       {
		if (o + 4 > len) return r;
		axis_rom_v2 = rd32(buf + o); o += 4;
		r.ax.axis_rom_v2 = axis_rom_v2;
	}
	if (schema >= 354) {
		u = read_cstring(buf, len, o, schema, 8192);
		if (!u.valid) return r;
		o = u.end;
	}
	if (schema >= 372) {
		if (o + 4 > len) return r;
		uint32_t fc = rd32(buf + o); o += 4;
		if (fc > 0x1000) return r;
		for (uint32_t i = 0; i < fc; i++) {
			struct cfr fr = read_cfolderref(buf, len, o, schema);
			if (!fr.valid) return r;
			o = fr.end;
		}
	}
	if (schema >= 440)      { if (o + 4 > len) return r; o += 4; }

	uint32_t rom = axis_rom;
	if (axis_type == 0) {
		rom = 0;
	} else if (!rom || rom == 0xFFFFFFFFu) {
		if (axis_rom_v2 && axis_rom_v2 != 0xFFFFFFFFu)
			rom = axis_rom_v2;
		else
			rom = 0;
	}

	r.ax.has_pts_address = rom > 0 && rom != 0xFFFFFFFFu;
	r.ax.pts_address     = rom;
	r.ax.pts_data_type   = axis_dt;
	r.ax.pts_data_size   = axis_dt ? bytes_from_dtype(axis_dt)
					: cb_to_bytes(cell_bits);
	r.ax.pts_signed      = flag3;
	r.ax.pts_big_endian  = is_be_dtype(axis_dt);
	r.ax.pts_count       = expected_sz;
	r.ax.scale           = scale;
	r.ax.offset          = offset;
	r.ax.has_scaling     = (scale != 1.0 || offset != 0.0);
	r.ax.source_axis_end = o;
	r.end                = o;
	r.valid              = 1;
	return r;
}

static void classify_dims(uint32_t kt, uint32_t a276, uint32_t a280,
			  uint32_t a396, uint32_t a400, int *x, int *y)
{
	if (a396 > DIM_A396_MAX) a396 = 0;
	if (a400 > DIM_A400_MAX) a400 = 0;
	if (a276 > DIM_A276_MAX) a276 = 0;

	if (kt == 1 || kt == 2) { *x = 1; *y = 1; return; }
	if (kt == 3) {
		*y = 1;
		*x = a396 > 0 ? (int)a396
			      : ((a276 > a280 ? (int)a276 : (int)a280) ?: 1);
		return;
	}
	if (kt == 5) {
		if (a400 > 0 || a396 > 0) {
			*x = a400 > 0 ? (int)a400 : 1;
			*y = a396 > 0 ? (int)a396 : 1;
			return;
		}
		*x = a280 > 0 ? (int)a280 : 1;
		*y = a276 > 0 ? (int)a276 : 1;
		return;
	}
	if (a400 > 0 && a396 > 0) { *x = (int)a400; *y = (int)a396; return; }
	if (a280 > 0 || a276 > 0) {
		*x = a280 > 0 ? (int)a280 : 1;
		*y = a276 > 0 ? (int)a276 : 1;
		return;
	}
	*x = 1; *y = 1;
}

static int is_envelope_string(const char *s)
{
	static const char *bl[] = {
		"(DAMOS)", "OLS 5.0 (Windows)", "Hexdump", "My maps",
		"Original", "Passenger car", "Engine", "Binary data",
		"0 (Original)", "Version 1", NULL,
	};
	for (int i = 0; bl[i]; i++)
		if (!strcmp(s, bl[i]))
			return 1;
	return 0;
}

static int parse_map(const uint8_t *buf, size_t len, size_t comment_off,
		     size_t hard_limit, uint32_t schema, struct ols_map *mi,
		     size_t *next_off)
{
	struct cur c = { buf, len, comment_off, schema };
	struct cfr cm;
	struct pstr n;
	uint32_t kt, cell_dt, cell_bits;
	uint32_t a276, a280, a396, a400;
	int x_sz, y_sz;
	struct pstr label, unit;
	double scale, offset;
	uint32_t rom_addr, rom_end;
	struct axis_res rows_ax, cols_ax;
	size_t trailer_start, trailer_end;
	long trailer = -1;
	int cell_signed;

	memset(mi, 0, sizeof(*mi));

	cm = cur_cfr(&c);
	if (!cm.valid) return 0;
	if (is_envelope_string(cm.name)) return 0;
	if (!strncmp(cm.name, "Imported from", 13)) return 0;
	if (cm.name[0] && !is_text((const uint8_t *)cm.name, strlen(cm.name)))
		return 0;

	if (c.len - c.off < 20) return 0;
	kt        = mi->raw_header_fields[0] = cur_u32(&c);
	mi->raw_header_fields[1] = cur_u32(&c);
	cell_dt   = mi->raw_header_fields[2] = cur_u32(&c);
	mi->raw_header_fields[3] = cur_u32(&c);
	cell_bits = mi->raw_header_fields[4] = cur_u32(&c);
	if (cell_bits != 2 && cell_bits != 10 && cell_bits != 16)
		cell_bits = 10;

	if (schema < 80) return 0;
	if (c.len - c.off < 8) return 0;
	mi->raw_header_fields[5] = cur_u32(&c);
	n = cur_cstr(&c, 8192);
	if (!n.valid || !is_ident(n.text, strlen(n.text))) return 0;

	if (schema >= 298) cur_u32(&c);
	if (schema >= 299) cur_u32(&c);
	if (schema >= 94)  mi->raw_u94 = cur_u32(&c);
	if (schema >= 74)  mi->raw_flag74 = cur_u8(&c);
	if (schema >= 123) cur_u32(&c);

	if (schema >= 300) {
		for (int i = 0; i < 6; i++) cur_f64(&c);
		if (schema >= 67) c.off += 48;
	} else if (schema >= 67) {
		if (!cur_need(&c, 48)) return 0;
		memcpy(mi->raw_display_bytes, buf + c.off, 48);
		c.off += 48;
	} else if (schema >= 66) {
		c.off += 16;
	} else if (schema < 59) {
		c.off += 8;
	}

	mi->raw_flags[0] = cur_u8(&c);
	mi->raw_flags[1] = cur_u8(&c);
	cell_signed = mi->raw_flags[1] != 0;
	mi->raw_flags[2] = cur_u8(&c);
	mi->raw_flags[3] = cur_u8(&c);

	a276 = mi->raw_geometry[0] = cur_u32(&c);
	a280 = mi->raw_geometry[1] = cur_u32(&c);
	a396 = mi->raw_geometry[2] = cur_u32(&c);
	a400 = mi->raw_geometry[3] = cur_u32(&c);
	mi->raw_geometry[4] = cur_u32(&c);

	classify_dims(kt, a276, a280, a396, a400, &x_sz, &y_sz);
	if (x_sz > (int)DIM_A400_MAX || y_sz > (int)DIM_A396_MAX ||
	    x_sz <= 0 || y_sz <= 0)
		return 0;

	label = cur_cstr(&c, 8192);
	unit  = cur_cstr(&c, 8192);
	if (!label.valid || !unit.valid) return 0;

	scale    = cur_f64(&c);
	offset   = cur_f64(&c);
	rom_addr = cur_u32(&c);
	rom_end  = cur_u32(&c);
	mi->rom_base = cur_u32(&c);

	if (schema >= 264) cur_f64(&c);
	if (schema >= 61)  mi->raw_u61 = cur_u32(&c);
	if (schema >= 105) { for (int i = 0; i < 4; i++) mi->raw_u105[i] = cur_u32(&c); }

	rows_ax = read_axis(buf, len, c.off, schema, y_sz);
	if (!rows_ax.valid) return 0;
	c.off = rows_ax.end;

	cols_ax = read_axis(buf, len, c.off, schema, x_sz);
	if (!cols_ax.valid) return 0;
	c.off = cols_ax.end;

	trailer_start = c.off;
	trailer_end = trailer_start + 0x800;
	if (trailer_end > hard_limit) trailer_end = hard_limit;
	for (size_t i = trailer_start; i + 3 <= trailer_end; i++) {
		if (buf[i] == 0x01 && buf[i+1] == 0x01 && buf[i+2] == 0x01) {
			trailer = (long)i;
			break;
		}
	}

	size_t rec_end = trailer >= 0 ? (size_t)trailer + 3 : c.off;
	mi->source_record_offset = comment_off;
	mi->source_record_end = rec_end;
	mi->kennfeld_type = kt;

	CPY_STR(mi->name, n.text);
	CPY_STR(mi->description, cm.name);
	CPY_STR(mi->unit, unit.text[0] ? unit.text : label.text);
	mi->raw_address = rom_addr;
	mi->address     = rom_addr;
	mi->rom_end     = rom_end;
	mi->nx = x_sz;
	mi->ny = y_sz;

	int cb;
	if (cell_dt) {
		cb = bytes_from_dtype(cell_dt);
	} else {
		cb = cb_to_bytes(cell_bits);
		if (rom_addr > 0 && rom_end > rom_addr && x_sz > 0 && y_sz > 0) {
			uint32_t dbytes = rom_end - rom_addr;
			int cells = x_sz * y_sz;
			int cs = (int)(dbytes / (uint32_t)cells);
			if (cs == 1 || cs == 2 || cs == 4) cb = cs;
		}
	}
	mi->data_size      = cb;
	mi->cell_data_type = cell_dt;
	mi->cell_big_endian= is_be_dtype(cell_dt);
	mi->column_major   = 1;
	mi->column_major_is_assumed = 1;

	if (cell_dt) {
		mi->data_signed = cell_signed;
	} else {
		mi->data_signed = (cb == 1 && cell_bits == 10) || offset < 0;
	}

	if (x_sz <= 1 && y_sz <= 1)      mi->type = "VALUE";
	else if (y_sz <= 1)               mi->type = "CURVE";
	else                              mi->type = "MAP";

	mi->scale  = scale;
	mi->offset = offset;
	mi->has_scaling = scale != 1.0 || offset != 0.0;

	mi->raw_rows_axis = rows_ax.ax;
	mi->raw_cols_axis = cols_ax.ax;
	if (kt == 3 && y_sz == 1) {
		mi->x_axis = rows_ax.ax;
		mi->x_axis.pts_count = x_sz;
	} else {
		mi->x_axis = cols_ax.ax;
	}
	if (y_sz > 1) {
		mi->y_axis = rows_ax.ax;
		mi->has_y_axis = 1;
	}

	*next_off = rec_end;
	return 1;
}

static void walk_maps(const uint8_t *buf, size_t len, size_t rstart, size_t rend,
		      uint32_t schema, struct ols_ctx *ctx)
{
	struct ols_map *arr = NULL;
	size_t cap = 0;

	size_t off = rstart;
	while (off + 8 < rend) {
		struct ols_map m;
		size_t next;
		if (parse_map(buf, len, off, rend, schema, &m, &next)) {
			if (ctx->nmaps == cap) {
				cap = cap ? cap * 2 : 64;
				struct ols_map *r = realloc(arr, cap * sizeof(*arr));
				if (!r) break;
				arr = r;
			}
			arr[ctx->nmaps++] = m;
			off = next;
			/* Empty descriptions are valid CString records. Do not skip
			 * them while looking for the next nonempty description. */
			continue;
		}
		off++;
	}
	ctx->maps = arr;
}

static void find_map_region(const uint8_t *buf, size_t len, uint32_t schema,
			    size_t *rstart, size_t *rend)
{
	*rend = len;
	long j = find_u32_le(buf, len, POSTZIP_HDR, 0);
	if (j > 0) *rend = (size_t)j;

	*rstart = 0x180;
	size_t scan_end = *rend > 32 ? *rend - 32 : *rend;
	if (scan_end > 0x10000) scan_end = 0x10000;
	for (size_t off = 0x180; off < scan_end; off++) {
		if (off + 4 >= *rend) break;
		uint32_t L = rd32(buf + off);
		if (L >= 4 && L <= 200 && off + 4 + L < *rend) {
			if (!is_text(buf + off + 4, L < 32 ? L : 32)) continue;
			struct ols_map m;
			size_t next;
			if (parse_map(buf, len, off, *rend, schema, &m, &next)) {
				*rstart = off;
				return;
			}
		}
	}
}

static int decode_axis_breakpoints(struct ols_axis *a, const uint8_t *rom,
				   size_t rom_len, uint32_t base_addr)
{
	if (!a->has_pts_address || a->pts_count <= 0)
		return 0;
	uint32_t addr = a->pts_address;
	if (addr < base_addr) return 0;
	size_t off = addr - base_addr;
	int cb = a->pts_data_size > 0 ? a->pts_data_size : 2;
	if (cb != 1 && cb != 2 && cb != 4 && cb != 8) cb = 2;
	if (off + (size_t)cb * a->pts_count > rom_len) return 0;

	a->pts_values = calloc(a->pts_count, sizeof(double));
	if (!a->pts_values) return 0;
	a->npts_values = a->pts_count;

	for (int i = 0; i < a->pts_count; i++) {
		const uint8_t *p = rom + off + (size_t)i * cb;
		double v = 0;
		uint32_t dt = a->pts_data_type;
		int sgn = a->pts_signed;

		switch (dt) {
		case 2: { uint16_t u = ((uint16_t)p[0] << 8) | p[1];
		          v = sgn ? (double)(int16_t)u : (double)u; break; }
		case 3: { uint16_t u = (uint16_t)p[0] | ((uint16_t)p[1] << 8);
		          v = sgn ? (double)(int16_t)u : (double)u; break; }
		case 4: { uint32_t u = ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
		                       ((uint32_t)p[2] << 8)  |  (uint32_t)p[3];
		          v = sgn ? (double)(int32_t)u : (double)u; break; }
		case 5: { uint32_t u = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
		                       ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
		          v = sgn ? (double)(int32_t)u : (double)u; break; }
		case 7: { float f; memcpy(&f, p, 4); v = f; break; }
		case 13: { double f; memcpy(&f, p, 8); v = f; break; }
		case 1: case 8: case 9: {
			v = sgn ? (double)(int8_t)p[0] : (double)p[0]; break;
		}
		default:
			switch (cb) {
			case 1: v = sgn ? (double)(int8_t)p[0] : (double)p[0]; break;
			case 2: { uint16_t u = (uint16_t)p[0] | ((uint16_t)p[1] << 8);
			          v = sgn ? (double)(int16_t)u : (double)u; break; }
			case 4: { uint32_t u = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
			                       ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
			          v = sgn ? (double)(int32_t)u : (double)u; break; }
			}
		}
		a->pts_values[i] = v;
	}
	return 0;
}

int ols_parse(const void *data, size_t len, struct ols_ctx *ctx)
{
	const uint8_t *buf = data;
	uint32_t fmt;
	size_t metadata_off;
	struct cur c;
	struct ols_segment *segs = NULL;
	size_t nsegs = 0;
	struct seg_group *groups = NULL;
	size_t ngroups = 0;
	size_t rstart, rend;

	memset(ctx, 0, sizeof(*ctx));

	if (header_parse(buf, len, &ctx->header, ctx) < 0)
		return -1;

	fmt = ctx->header.format_version;
	if (fmt >= MODERN_FMT_MIN && ctx->header.declared_size != (uint32_t)len)
		warn(ctx, "header size %u != actual %zu",
		     ctx->header.declared_size, len);

	metadata_off = (fmt >= MODERN_FMT_MIN) ? 0x18 : 0x14;
	c.buf = buf; c.len = len; c.off = metadata_off; c.schema = fmt;
	parse_metadata(&c, &ctx->metadata, ctx);

	scan_magic(buf, len, ctx->anchors);

	if (fmt >= MODERN_FMT_MIN) {
		segs = find_segments(buf, len, &nsegs);
		if (segs && nsegs > 0)
			classify_primaries(segs, nsegs);
	}

	if (!segs || nsegs == 0) {
		struct ols_segment seg;
		int nv = old_format_version(buf, len, &seg);
		if (nv > 0) {
			segs = malloc(sizeof(*segs) * (size_t)nv);
			if (segs) {
				for (int i = 0; i < nv; i++) {
					segs[i] = seg;
					segs[i].segment_index = 0;
					segs[i].data_start = seg.data_start +
						(size_t)i * seg.flash_size;
					segs[i].flash_base = (uint32_t)segs[i].data_start;
				}
				nsegs = (size_t)nv;
			}
		}
	}

	if (segs && nsegs > 0) {
		groups = group_by_version(segs, nsegs, &ngroups);
		if (!groups && nsegs > 0) {
			groups = malloc(sizeof(*groups));
			if (groups) {
				groups[0].segs = segs;
				groups[0].n = nsegs;
				ngroups = 1;
			}
		}
	}

	if (ngroups > 0) {
		ctx->versions = calloc(ngroups, sizeof(*ctx->versions));
		if (!ctx->versions) {
			free(segs); free(groups);
			return fail(ctx, "out of memory (versions)");
		}
		ctx->nversions = ngroups;
		for (size_t i = 0; i < ngroups; i++) {
			struct ols_version *v = &ctx->versions[i];
			v->index = (int)i;
			snprintf(v->name, sizeof(v->name), "Version %zu", i);
			v->segments = calloc(groups[i].n, sizeof(*v->segments));
			if (v->segments) {
				memcpy(v->segments, groups[i].segs,
				       groups[i].n * sizeof(*v->segments));
				v->nsegments = groups[i].n;
			}
			uint8_t *rom = NULL;
			size_t rom_sz = 0;
			uint32_t base = 0;
			assemble_rom(buf, len, groups[i].segs, groups[i].n,
				     &rom, &rom_sz, &base);
			v->rom_data = rom;
			v->rom_size = rom_sz;
			v->base_address = base;
		}
	}

	find_map_region(buf, len, fmt, &rstart, &rend);
	walk_maps(buf, len, rstart, rend, fmt, ctx);

	if (ctx->nversions > 0 && ctx->maps) {
		uint32_t base = ctx->versions[0].base_address;
		const uint8_t *rom = ctx->versions[0].rom_data;
		size_t rom_len = ctx->versions[0].rom_size;
		for (size_t i = 0; i < ctx->nmaps; i++) {
			struct ols_map *m = &ctx->maps[i];
			m->raw_address = m->address;
			if (m->raw_address >= base)
				m->address = m->raw_address - base;
			if (m->x_axis.has_pts_address && m->x_axis.pts_address >= base)
				m->x_axis.pts_address -= base;
			if (m->y_axis.has_pts_address && m->y_axis.pts_address >= base)
				m->y_axis.pts_address -= base;
			decode_axis_breakpoints(&m->x_axis, rom, rom_len, 0);
			if (m->has_y_axis)
				decode_axis_breakpoints(&m->y_axis, rom, rom_len, 0);
		}
	}

	free(segs);
	free(groups);
	return 0;
}

void ols_free(struct ols_ctx *ctx)
{
	for (size_t i = 0; i < ctx->nversions; i++) {
		free(ctx->versions[i].rom_data);
		free(ctx->versions[i].segments);
	}
	free(ctx->versions);
	for (size_t i = 0; i < ctx->nmaps; i++) {
		free(ctx->maps[i].x_axis.pts_values);
		free(ctx->maps[i].y_axis.pts_values);
	}
	free(ctx->maps);
	for (size_t i = 0; i < ctx->nwarnings; i++)
		free(ctx->warnings[i]);
	free(ctx->warnings);
	memset(ctx, 0, sizeof(*ctx));
}

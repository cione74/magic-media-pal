/* Regression for the exact FFmpeg HEVC count boundary (CVE-2026-75141).
 * Built in the candidate source tree; no malformed media is published.
 */
#include <assert.h>
#include "libavformat/hevc.c"

int main(void)
{
    const uint8_t nal[] = {0x40, 0x01, 0x00};
    HVCCNALUnitArray array = {0};
    array.numNalus = UINT16_MAX;
    assert(hvcc_array_add_nal_unit(nal, sizeof(nal), &array) == AVERROR_INVALIDDATA);
    assert(array.numNalus == UINT16_MAX && array.nal == NULL);

    array.numNalus = UINT16_MAX - 1;
    array.nal = av_calloc(UINT16_MAX, sizeof(*array.nal));
    assert(array.nal != NULL);
    assert(hvcc_array_add_nal_unit(nal, sizeof(nal), &array) == 0);
    assert(array.numNalus == UINT16_MAX);
    assert(array.nal[UINT16_MAX - 1].nalUnit == nal);
    assert(hvcc_array_add_nal_unit(nal, sizeof(nal), &array) == AVERROR_INVALIDDATA);
    assert(array.numNalus == UINT16_MAX);
    av_free(array.nal);
    return 0;
}

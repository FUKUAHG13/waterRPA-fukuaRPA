#define NOMINMAX
#include <windows.h>
#include <gdiplus.h>
#include <d3d11.h>
#include <dxgi1_2.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <climits>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <list>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

using namespace Gdiplus;

struct WrpaRect {
    int x;
    int y;
    int w;
    int h;
};

struct WrpaMatch {
    double x;
    double y;
    double scale;
    double score;
    double radius;
};

struct WrpaPerfStats {
    std::int64_t calls;
    std::int64_t captures;
    std::int64_t integral_builds;
    std::int64_t template_cache_hits;
    std::int64_t template_cache_misses;
    std::int64_t template_variants_built;
    std::int64_t work_budget_fallbacks;
    std::int64_t capture_microseconds;
    std::int64_t template_microseconds;
    std::int64_t match_microseconds;
    std::int64_t cache_bytes;
    std::int64_t cache_entries;
};

enum WrpaCapability : std::uint64_t {
    WRPA_CAP_GDI_CAPTURE = 1ull << 0,
    WRPA_CAP_MULTI_REGION = 1ull << 1,
    WRPA_CAP_MULTI_SCALE = 1ull << 2,
    WRPA_CAP_GRAYSCALE = 1ull << 3,
    WRPA_CAP_COLOR = 1ull << 4,
    WRPA_CAP_FIND_ALL = 1ull << 5,
    WRPA_CAP_TEMPLATE_LRU = 1ull << 6,
    WRPA_CAP_PERF_COUNTERS = 1ull << 7,
    WRPA_CAP_WORK_BUDGET = 1ull << 8,
    WRPA_CAP_SINGLE_CAPTURE_PER_REGION = 1ull << 9,
    WRPA_CAP_ABI_METADATA = 1ull << 10,
    WRPA_CAP_BOUNDED_JOB_POOL = 1ull << 11,
    WRPA_CAP_PREFERRED_SCALE_FALLBACK = 1ull << 12,
    WRPA_CAP_PREFERRED_SCALE_LIST = 1ull << 13,
    WRPA_CAP_EXPLICIT_SCALE_ONLY = 1ull << 14,
    WRPA_CAP_LOW_RES_SCENE_FINGERPRINT = 1ull << 15,
    WRPA_CAP_DXGI_SCENE_CHANGE = 1ull << 16,
    WRPA_CAP_CANCELLABLE_SEARCH = 1ull << 17,
    WRPA_CAP_OPENCV_COLOR_SEMANTICS = 1ull << 18,
    WRPA_CAP_RAW_RGB_CAPTURE = 1ull << 19,
};

enum WrpaBuildFlag : std::uint64_t {
    WRPA_BUILD_X64 = 1ull << 0,
    WRPA_BUILD_STATIC_CRT = 1ull << 1,
    WRPA_BUILD_CPP17 = 1ull << 2,
    WRPA_BUILD_WINDOWS10_TARGET = 1ull << 3,
    WRPA_BUILD_MSVC = 1ull << 4,
};

enum WrpaStructId : int {
    WRPA_STRUCT_RECT = 1,
    WRPA_STRUCT_MATCH = 2,
    WRPA_STRUCT_PERF_STATS = 3,
};

struct PixelImage {
    int w = 0;
    int h = 0;
    int channels = 1;
    std::vector<unsigned char> data;
};

struct CapturedRegion {
    WrpaRect rect{};
    PixelImage screen;
    std::vector<double> sum;
    std::vector<double> sqsum;
};

struct Candidate {
    double x = 0.0;
    double y = 0.0;
    double scale = 1.0;
    double score = -2.0;
    double radius = 4.0;
};

static std::atomic<std::int64_t> g_perf_calls{0};
static std::atomic<std::int64_t> g_perf_captures{0};
static std::atomic<std::int64_t> g_perf_integral_builds{0};
static std::atomic<std::int64_t> g_perf_template_cache_hits{0};
static std::atomic<std::int64_t> g_perf_template_cache_misses{0};
static std::atomic<std::int64_t> g_perf_template_variants_built{0};
static std::atomic<std::int64_t> g_perf_work_budget_fallbacks{0};
static std::atomic<std::int64_t> g_perf_capture_microseconds{0};
static std::atomic<std::int64_t> g_perf_template_microseconds{0};
static std::atomic<std::int64_t> g_perf_match_microseconds{0};
static std::atomic<std::int64_t> g_perf_cache_bytes{0};
static std::atomic<std::int64_t> g_perf_cache_entries{0};
static std::atomic<std::uint64_t> g_cancel_generation{0};

static bool search_cancelled(std::uint64_t generation) {
    return g_cancel_generation.load(std::memory_order_relaxed) != generation;
}

struct DesktopDuplicationTarget {
    IDXGIOutputDuplication* duplication = nullptr;
    ID3D11Device* device = nullptr;
    RECT desktop{};
};

static std::mutex g_desktop_duplication_mutex;
static std::vector<DesktopDuplicationTarget> g_desktop_duplications;

static void release_desktop_duplications() {
    for (DesktopDuplicationTarget& target : g_desktop_duplications) {
        if (target.duplication) {
            target.duplication->Release();
            target.duplication = nullptr;
        }
        if (target.device) {
            target.device->Release();
            target.device = nullptr;
        }
    }
    g_desktop_duplications.clear();
}

static bool initialize_desktop_duplications(std::wstring& err) {
    if (!g_desktop_duplications.empty()) {
        return true;
    }
    IDXGIFactory1* factory = nullptr;
    HRESULT result = CreateDXGIFactory1(
        __uuidof(IDXGIFactory1), reinterpret_cast<void**>(&factory)
    );
    if (FAILED(result) || !factory) {
        err = L"CreateDXGIFactory1 failed";
        return false;
    }

    for (UINT adapter_index = 0; ; ++adapter_index) {
        IDXGIAdapter1* adapter = nullptr;
        result = factory->EnumAdapters1(adapter_index, &adapter);
        if (result == DXGI_ERROR_NOT_FOUND) {
            break;
        }
        if (FAILED(result) || !adapter) {
            continue;
        }

        ID3D11Device* device = nullptr;
        ID3D11DeviceContext* context = nullptr;
        D3D_FEATURE_LEVEL feature_level{};
        result = D3D11CreateDevice(
            adapter,
            D3D_DRIVER_TYPE_UNKNOWN,
            nullptr,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            nullptr,
            0,
            D3D11_SDK_VERSION,
            &device,
            &feature_level,
            &context
        );
        if (context) {
            context->Release();
        }
        if (FAILED(result) || !device) {
            adapter->Release();
            continue;
        }

        for (UINT output_index = 0; ; ++output_index) {
            IDXGIOutput* output = nullptr;
            result = adapter->EnumOutputs(output_index, &output);
            if (result == DXGI_ERROR_NOT_FOUND) {
                break;
            }
            if (FAILED(result) || !output) {
                continue;
            }
            DXGI_OUTPUT_DESC description{};
            if (
                FAILED(output->GetDesc(&description))
                || !description.AttachedToDesktop
            ) {
                output->Release();
                continue;
            }
            IDXGIOutput1* output1 = nullptr;
            result = output->QueryInterface(
                __uuidof(IDXGIOutput1),
                reinterpret_cast<void**>(&output1)
            );
            output->Release();
            if (FAILED(result) || !output1) {
                continue;
            }
            IDXGIOutputDuplication* duplication = nullptr;
            result = output1->DuplicateOutput(device, &duplication);
            output1->Release();
            if (FAILED(result) || !duplication) {
                continue;
            }
            device->AddRef();
            g_desktop_duplications.push_back(
                DesktopDuplicationTarget{
                    duplication,
                    device,
                    description.DesktopCoordinates,
                }
            );
        }
        device->Release();
        adapter->Release();
    }
    factory->Release();

    if (g_desktop_duplications.empty()) {
        err = L"no desktop duplication output available";
        return false;
    }
    return true;
}

static bool rects_intersect(const RECT& first, const WrpaRect& second) {
    return first.left < second.x + second.w
        && first.right > second.x
        && first.top < second.y + second.h
        && first.bottom > second.y;
}

static bool change_is_watched(
    const RECT& changed,
    const WrpaRect* regions,
    int region_count
) {
    if (!regions || region_count <= 0) {
        return true;
    }
    for (int index = 0; index < region_count; ++index) {
        if (
            regions[index].w > 0
            && regions[index].h > 0
            && rects_intersect(changed, regions[index])
        ) {
            return true;
        }
    }
    return false;
}

static int poll_desktop_duplications(
    const WrpaRect* regions,
    int region_count,
    bool reset_baseline,
    std::wstring& err
) {
    if (!initialize_desktop_duplications(err)) {
        return -1;
    }
    bool watched_change = false;
    bool access_lost = false;
    for (DesktopDuplicationTarget& target : g_desktop_duplications) {
        DXGI_OUTDUPL_FRAME_INFO frame_info{};
        IDXGIResource* resource = nullptr;
        const HRESULT acquired = target.duplication->AcquireNextFrame(
            0, &frame_info, &resource
        );
        if (acquired == DXGI_ERROR_WAIT_TIMEOUT) {
            continue;
        }
        if (acquired == DXGI_ERROR_ACCESS_LOST) {
            access_lost = true;
            continue;
        }
        if (FAILED(acquired)) {
            continue;
        }

        bool output_changed = false;
        UINT required = 0;
        if (frame_info.TotalMetadataBufferSize > 0) {
            target.duplication->GetFrameDirtyRects(0, nullptr, &required);
            if (required > 0) {
                std::vector<unsigned char> buffer(required);
                if (SUCCEEDED(target.duplication->GetFrameDirtyRects(
                    required,
                    reinterpret_cast<RECT*>(buffer.data()),
                    &required
                ))) {
                    const int count = static_cast<int>(required / sizeof(RECT));
                    const RECT* rects = reinterpret_cast<const RECT*>(buffer.data());
                    for (int index = 0; index < count; ++index) {
                        RECT changed = rects[index];
                        OffsetRect(
                            &changed, target.desktop.left, target.desktop.top
                        );
                        if (change_is_watched(changed, regions, region_count)) {
                            output_changed = true;
                            break;
                        }
                    }
                }
            }
            required = 0;
            target.duplication->GetFrameMoveRects(0, nullptr, &required);
            if (!output_changed && required > 0) {
                std::vector<unsigned char> buffer(required);
                if (SUCCEEDED(target.duplication->GetFrameMoveRects(
                    required,
                    reinterpret_cast<DXGI_OUTDUPL_MOVE_RECT*>(buffer.data()),
                    &required
                ))) {
                    const int count = static_cast<int>(
                        required / sizeof(DXGI_OUTDUPL_MOVE_RECT)
                    );
                    const DXGI_OUTDUPL_MOVE_RECT* rects =
                        reinterpret_cast<const DXGI_OUTDUPL_MOVE_RECT*>(
                            buffer.data()
                        );
                    for (int index = 0; index < count; ++index) {
                        RECT changed = rects[index].DestinationRect;
                        OffsetRect(
                            &changed, target.desktop.left, target.desktop.top
                        );
                        if (change_is_watched(changed, regions, region_count)) {
                            output_changed = true;
                            break;
                        }
                    }
                }
            }
        }
        if (!output_changed && frame_info.LastPresentTime.QuadPart != 0) {
            output_changed = change_is_watched(
                target.desktop, regions, region_count
            );
        }

        if (resource) {
            resource->Release();
        }
        target.duplication->ReleaseFrame();
        if (!reset_baseline && output_changed) {
            watched_change = true;
        }
    }
    // ACCESS_LOST: discard any changes detected this round (conservative).
    // Rationale: duplication must be re-created; stale change flags from
    // a lost session are unreliable. Delaying wake by one poll (~30-70ms)
    // is acceptable since scene wake is a pure optimization path.
    if (access_lost) {
        release_desktop_duplications();
        err = L"desktop duplication access lost";
        return -1;
    }
    return watched_change ? 1 : 0;
}

static std::int64_t elapsed_microseconds(
    const std::chrono::steady_clock::time_point& started
) {
    return std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::steady_clock::now() - started
    ).count();
}

static void set_error(wchar_t* err, int err_len, const std::wstring& text) {
    if (!err || err_len <= 0) {
        return;
    }
    wcsncpy_s(err, static_cast<size_t>(err_len), text.c_str(), _TRUNCATE);
}

static bool ensure_gdiplus(std::wstring& err) {
    static std::once_flag once;
    static Status status = GenericError;
    static ULONG_PTR token = 0;
    std::call_once(once, []() {
        GdiplusStartupInput input;
        // GDI+ is initialized once per process lifetime. No GdiplusShutdown needed:
        // the DLL is never unloaded before process exit (OS reclaims all resources).
        status = GdiplusStartup(&token, &input, nullptr);
    });
    if (status != Ok) {
        err = L"GDI+ startup failed";
        return false;
    }
    return true;
}

static unsigned char to_gray(unsigned char r, unsigned char g, unsigned char b) {
    return static_cast<unsigned char>((77u * r + 150u * g + 29u * b) >> 8);
}

static bool bitmap_to_image(Bitmap& bitmap, double scale, bool use_gray, PixelImage& out, std::wstring& err) {
    const int src_w = static_cast<int>(bitmap.GetWidth());
    const int src_h = static_cast<int>(bitmap.GetHeight());
    if (src_w <= 0 || src_h <= 0 || scale <= 0.0) {
        err = L"invalid template size";
        return false;
    }

    const int dst_w = std::max(1, static_cast<int>(std::llround(src_w * scale)));
    const int dst_h = std::max(1, static_cast<int>(std::llround(src_h * scale)));

    Bitmap scaled(dst_w, dst_h, PixelFormat32bppARGB);
    Graphics graphics(&scaled);
    graphics.SetPixelOffsetMode(PixelOffsetModeHalf);
    graphics.SetInterpolationMode(InterpolationModeHighQualityBicubic);
    graphics.DrawImage(&bitmap, 0, 0, dst_w, dst_h);

    Rect rect(0, 0, dst_w, dst_h);
    BitmapData bits;
    if (scaled.LockBits(&rect, ImageLockModeRead, PixelFormat32bppARGB, &bits) != Ok) {
        err = L"template lock failed";
        return false;
    }

    out.w = dst_w;
    out.h = dst_h;
    out.channels = use_gray ? 1 : 3;
    out.data.assign(static_cast<size_t>(dst_w) * dst_h * out.channels, 0);

    const int stride = bits.Stride;
    const unsigned char* base = static_cast<const unsigned char*>(bits.Scan0);
    for (int y = 0; y < dst_h; ++y) {
        const unsigned char* row = stride >= 0
            ? base + static_cast<size_t>(y) * stride
            : base + static_cast<size_t>(dst_h - 1 - y) * (-stride);
        for (int x = 0; x < dst_w; ++x) {
            const unsigned char* px = row + static_cast<size_t>(x) * 4;
            const unsigned char b = px[0];
            const unsigned char g = px[1];
            const unsigned char r = px[2];
            const size_t out_idx = (static_cast<size_t>(y) * dst_w + x) * out.channels;
            if (use_gray) {
                out.data[out_idx] = to_gray(r, g, b);
            } else {
                out.data[out_idx + 0] = b;
                out.data[out_idx + 1] = g;
                out.data[out_idx + 2] = r;
            }
        }
    }

    scaled.UnlockBits(&bits);
    return true;
}

static bool capture_rect(const WrpaRect& rect, bool use_gray, PixelImage& out, std::wstring& err) {
    if (rect.w <= 0 || rect.h <= 0) {
        err = L"invalid capture rect";
        return false;
    }

    HDC screen_dc = GetDC(nullptr);
    if (!screen_dc) {
        err = L"GetDC failed";
        return false;
    }

    HDC mem_dc = CreateCompatibleDC(screen_dc);
    if (!mem_dc) {
        ReleaseDC(nullptr, screen_dc);
        err = L"CreateCompatibleDC failed";
        return false;
    }

    BITMAPINFO bmi;
    ZeroMemory(&bmi, sizeof(bmi));
    bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bmi.bmiHeader.biWidth = rect.w;
    bmi.bmiHeader.biHeight = -rect.h;
    bmi.bmiHeader.biPlanes = 1;
    bmi.bmiHeader.biBitCount = 32;
    bmi.bmiHeader.biCompression = BI_RGB;

    void* raw_bits = nullptr;
    HBITMAP dib = CreateDIBSection(screen_dc, &bmi, DIB_RGB_COLORS, &raw_bits, nullptr, 0);
    if (!dib || !raw_bits) {
        DeleteDC(mem_dc);
        ReleaseDC(nullptr, screen_dc);
        err = L"CreateDIBSection failed";
        return false;
    }

    HGDIOBJ old_obj = SelectObject(mem_dc, dib);
    BOOL ok = BitBlt(mem_dc, 0, 0, rect.w, rect.h, screen_dc, rect.x, rect.y, SRCCOPY | CAPTUREBLT);
    SelectObject(mem_dc, old_obj);

    if (!ok) {
        DeleteObject(dib);
        DeleteDC(mem_dc);
        ReleaseDC(nullptr, screen_dc);
        err = L"BitBlt failed";
        return false;
    }

    out.w = rect.w;
    out.h = rect.h;
    out.channels = use_gray ? 1 : 3;
    out.data.assign(static_cast<size_t>(rect.w) * rect.h * out.channels, 0);

    const unsigned char* pixels = static_cast<const unsigned char*>(raw_bits);
    for (int y = 0; y < rect.h; ++y) {
        for (int x = 0; x < rect.w; ++x) {
            const unsigned char* px = pixels + (static_cast<size_t>(y) * rect.w + x) * 4;
            const unsigned char b = px[0];
            const unsigned char g = px[1];
            const unsigned char r = px[2];
            const size_t out_idx = (static_cast<size_t>(y) * rect.w + x) * out.channels;
            if (use_gray) {
                out.data[out_idx] = to_gray(r, g, b);
            } else {
                out.data[out_idx + 0] = b;
                out.data[out_idx + 1] = g;
                out.data[out_idx + 2] = r;
            }
        }
    }

    DeleteObject(dib);
    DeleteDC(mem_dc);
    ReleaseDC(nullptr, screen_dc);
    return true;
}

static bool capture_gray_fingerprint(
    const WrpaRect& rect,
    int target_w,
    int target_h,
    std::vector<unsigned char>& out,
    std::wstring& err
) {
    if (
        rect.w <= 0 || rect.h <= 0
        || target_w <= 0 || target_h <= 0
        || target_w > 512 || target_h > 512
    ) {
        err = L"invalid fingerprint dimensions";
        return false;
    }

    HDC screen_dc = GetDC(nullptr);
    if (!screen_dc) {
        err = L"GetDC failed";
        return false;
    }
    HDC mem_dc = CreateCompatibleDC(screen_dc);
    if (!mem_dc) {
        ReleaseDC(nullptr, screen_dc);
        err = L"CreateCompatibleDC failed";
        return false;
    }

    BITMAPINFO bmi;
    ZeroMemory(&bmi, sizeof(bmi));
    bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bmi.bmiHeader.biWidth = target_w;
    bmi.bmiHeader.biHeight = -target_h;
    bmi.bmiHeader.biPlanes = 1;
    bmi.bmiHeader.biBitCount = 32;
    bmi.bmiHeader.biCompression = BI_RGB;

    void* raw_bits = nullptr;
    HBITMAP dib = CreateDIBSection(
        screen_dc, &bmi, DIB_RGB_COLORS, &raw_bits, nullptr, 0
    );
    if (!dib || !raw_bits) {
        if (dib) {
            DeleteObject(dib);
        }
        DeleteDC(mem_dc);
        ReleaseDC(nullptr, screen_dc);
        err = L"CreateDIBSection failed";
        return false;
    }

    HGDIOBJ old_obj = SelectObject(mem_dc, dib);
    // COLORONCOLOR is intentionally used here: this is a coarse change
    // fingerprint, and HALFTONE scans far more source pixels on large desktops.
    SetStretchBltMode(mem_dc, COLORONCOLOR);
    const auto capture_started = std::chrono::steady_clock::now();
    g_perf_captures.fetch_add(1, std::memory_order_relaxed);
    const BOOL ok = StretchBlt(
        mem_dc,
        0,
        0,
        target_w,
        target_h,
        screen_dc,
        rect.x,
        rect.y,
        rect.w,
        rect.h,
        SRCCOPY | CAPTUREBLT
    );
    g_perf_capture_microseconds.fetch_add(
        elapsed_microseconds(capture_started), std::memory_order_relaxed
    );
    SelectObject(mem_dc, old_obj);

    if (!ok) {
        DeleteObject(dib);
        DeleteDC(mem_dc);
        ReleaseDC(nullptr, screen_dc);
        err = L"StretchBlt failed";
        return false;
    }

    out.assign(static_cast<size_t>(target_w) * target_h, 0);
    const unsigned char* pixels = static_cast<const unsigned char*>(raw_bits);
    for (int index = 0; index < target_w * target_h; ++index) {
        const unsigned char* pixel = pixels + static_cast<size_t>(index) * 4;
        out[static_cast<size_t>(index)] = to_gray(
            pixel[2], pixel[1], pixel[0]
        );
    }

    DeleteObject(dib);
    DeleteDC(mem_dc);
    ReleaseDC(nullptr, screen_dc);
    return true;
}

static void build_integral(
    const PixelImage& screen,
    std::vector<double>& sum,
    std::vector<double>& sqsum
) {
    const int w = screen.w;
    const int h = screen.h;
    const int ch = screen.channels;
    const int stride = w + 1;
    const size_t plane_size = static_cast<size_t>(stride) * (h + 1);
    sum.assign(plane_size * ch, 0.0);
    sqsum.assign(plane_size * ch, 0.0);

    // OpenCV's color TM_CCOEFF_NORMED subtracts a separate mean from each
    // channel. Keeping one integral plane per channel preserves that contract.
    for (int c = 0; c < ch; ++c) {
        const size_t plane_offset = plane_size * c;
        for (int y = 1; y <= h; ++y) {
            double row_sum = 0.0;
            double row_sq = 0.0;
            for (int x = 1; x <= w; ++x) {
                const size_t px_idx = (
                    static_cast<size_t>(y - 1) * w + (x - 1)
                ) * ch + c;
                const double value = static_cast<double>(screen.data[px_idx]);
                row_sum += value;
                row_sq += value * value;
                const size_t idx = plane_offset
                    + static_cast<size_t>(y) * stride + x;
                sum[idx] = sum[idx - stride] + row_sum;
                sqsum[idx] = sqsum[idx - stride] + row_sq;
            }
        }
    }
}

static double rect_integral_sum(
    const std::vector<double>& integral,
    int stride,
    size_t plane_size,
    int channel,
    int x,
    int y,
    int w,
    int h
) {
    const int x2 = x + w;
    const int y2 = y + h;
    const size_t offset = plane_size * channel;
    return integral[offset + static_cast<size_t>(y2) * stride + x2]
        - integral[offset + static_cast<size_t>(y) * stride + x2]
        - integral[offset + static_cast<size_t>(y2) * stride + x]
        + integral[offset + static_cast<size_t>(y) * stride + x];
}

static bool template_stats(
    const PixelImage& templ,
    std::array<double, 3>& means,
    double& var
) {
    const size_t pixel_count = static_cast<size_t>(templ.w) * templ.h;
    if (pixel_count == 0 || templ.channels < 1 || templ.channels > 3) {
        return false;
    }
    std::array<double, 3> sums{};
    std::array<double, 3> square_sums{};
    for (size_t pixel = 0; pixel < pixel_count; ++pixel) {
        for (int channel = 0; channel < templ.channels; ++channel) {
            const double value = static_cast<double>(
                templ.data[pixel * templ.channels + channel]
            );
            sums[channel] += value;
            square_sums[channel] += value * value;
        }
    }
    means.fill(0.0);
    var = 0.0;
    const double count = static_cast<double>(pixel_count);
    for (int channel = 0; channel < templ.channels; ++channel) {
        means[channel] = sums[channel] / count;
        var += square_sums[channel] - (sums[channel] * sums[channel] / count);
    }
    return var > 1e-6;
}

static double dot_at(const PixelImage& screen, const PixelImage& templ, int x, int y) {
    const int sw = screen.w;
    const int tw = templ.w;
    const int th = templ.h;
    const int ch = screen.channels;
    double dot = 0.0;
    for (int ty = 0; ty < th; ++ty) {
        const unsigned char* s = screen.data.data() + ((static_cast<size_t>(y + ty) * sw + x) * ch);
        const unsigned char* t = templ.data.data() + (static_cast<size_t>(ty) * tw * ch);
        const int count = tw * ch;
        for (int i = 0; i < count; ++i) {
            dot += static_cast<double>(s[i]) * static_cast<double>(t[i]);
        }
    }
    return dot;
}

static void trim_candidates(std::vector<Candidate>& candidates, size_t keep) {
    if (candidates.size() <= keep) {
        return;
    }
    std::nth_element(
        candidates.begin(),
        candidates.begin() + static_cast<std::ptrdiff_t>(keep),
        candidates.end(),
        [](const Candidate& a, const Candidate& b) { return a.score > b.score; }
    );
    candidates.resize(keep);
}

struct MatchPlan {
    const CapturedRegion* captured = nullptr;
    const PixelImage* templ = nullptr;
    double scale = 1.0;
    std::array<double, 3> templ_means{};
    double templ_var = 0.0;
    double pixel_count = 0.0;
    double radius = 4.0;
    double work = 0.0;
    int result_w = 0;
    int result_h = 0;
    int integral_stride = 0;
    size_t integral_plane_size = 0;
};

struct MatchJob {
    size_t plan_index = 0;
    int y0 = 0;
    int y1 = 0;
};

static bool candidate_better(const Candidate& a, const Candidate& b) {
    if (a.score != b.score) {
        return a.score > b.score;
    }
    if (a.y != b.y) {
        return a.y < b.y;
    }
    if (a.x != b.x) {
        return a.x < b.x;
    }
    return a.scale < b.scale;
}

static bool build_match_plan(
    const CapturedRegion& captured,
    const PixelImage& templ,
    double scale,
    MatchPlan& out
) {
    if (
        captured.screen.channels != templ.channels
        || templ.w <= 0
        || templ.h <= 0
        || templ.w > captured.screen.w
        || templ.h > captured.screen.h
    ) {
        return false;
    }

    std::array<double, 3> templ_means{};
    double templ_var = 0.0;
    if (!template_stats(templ, templ_means, templ_var)) {
        return false;
    }

    out.captured = &captured;
    out.templ = &templ;
    out.scale = scale;
    out.templ_means = templ_means;
    out.templ_var = templ_var;
    out.pixel_count = static_cast<double>(templ.w) * templ.h;
    out.radius = std::max(4.0, std::min(templ.w, templ.h) * 0.55);
    out.result_w = captured.screen.w - templ.w + 1;
    out.result_h = captured.screen.h - templ.h + 1;
    out.integral_stride = captured.screen.w + 1;
    out.integral_plane_size = static_cast<size_t>(out.integral_stride)
        * (captured.screen.h + 1);
    out.work = static_cast<double>(out.result_w) * out.result_h
        * templ.w * templ.h * templ.channels;
    return true;
}

static void match_job(
    const MatchPlan& plan,
    const MatchJob& job,
    double threshold,
    bool find_all,
    int max_matches,
    std::vector<Candidate>& out,
    std::uint64_t cancel_generation
) {
    const CapturedRegion& captured = *plan.captured;
    const PixelImage& screen = captured.screen;
    const PixelImage& templ = *plan.templ;
    const size_t keep = static_cast<size_t>(std::max(64, max_matches * 8));
    out.reserve(find_all ? std::min<size_t>(keep, 128) : 1);
    Candidate best;

    for (int y = job.y0; y < job.y1; ++y) {
        if (search_cancelled(cancel_generation)) {
            return;
        }
        for (int x = 0; x < plan.result_w; ++x) {
            if ((x & 31) == 0 && search_cancelled(cancel_generation)) {
                return;
            }
            double svar = 0.0;
            double mean_correction = 0.0;
            for (int channel = 0; channel < screen.channels; ++channel) {
                const double ssum = rect_integral_sum(
                    captured.sum,
                    plan.integral_stride,
                    plan.integral_plane_size,
                    channel,
                    x,
                    y,
                    templ.w,
                    templ.h
                );
                const double ssq = rect_integral_sum(
                    captured.sqsum,
                    plan.integral_stride,
                    plan.integral_plane_size,
                    channel,
                    x,
                    y,
                    templ.w,
                    templ.h
                );
                svar += ssq - (ssum * ssum / plan.pixel_count);
                mean_correction += plan.templ_means[channel] * ssum;
            }
            if (svar <= 1e-6) {
                continue;
            }

            const double dot = dot_at(screen, templ, x, y);
            const double cov = dot - mean_correction;
            const double score = std::max(
                -1.0,
                std::min(1.0, cov / std::sqrt(plan.templ_var * svar))
            );
            Candidate candidate{
                static_cast<double>(captured.rect.x + x + templ.w / 2.0),
                static_cast<double>(captured.rect.y + y + templ.h / 2.0),
                plan.scale,
                score,
                plan.radius
            };

            if (find_all) {
                if (score >= threshold) {
                    out.push_back(candidate);
                    if (out.size() > keep * 2) {
                        trim_candidates(out, keep);
                    }
                }
            } else if (candidate_better(candidate, best)) {
                best = candidate;
            }
        }
    }

    if (!find_all && best.score >= threshold) {
        out.push_back(best);
    } else if (find_all) {
        trim_candidates(out, keep);
    }
}

static int choose_worker_count(
    const std::vector<MatchPlan>& plans,
    int parallel_mode
) {
    if (parallel_mode <= 0 || plans.empty()) {
        return 1;
    }
    const unsigned int hardware = std::max(1u, std::thread::hardware_concurrency());
    const int maximum = std::max(1, std::min<int>(8, static_cast<int>(hardware)));
    if (parallel_mode >= 2) {
        return maximum;
    }
    double total_work = 0.0;
    for (const MatchPlan& plan : plans) {
        total_work += plan.work;
    }
    const int useful = std::max(
        1, static_cast<int>(std::ceil(total_work / 4000000.0))
    );
    return std::min(maximum, useful);
}

static bool run_match_plans(
    const std::vector<MatchPlan>& plans,
    int parallel_mode,
    double threshold,
    bool find_all,
    int max_matches,
    std::vector<Candidate>& out,
    std::uint64_t cancel_generation
) {
    if (plans.empty()) {
        return !search_cancelled(cancel_generation);
    }

    const int worker_count = choose_worker_count(plans, parallel_mode);
    double total_work = 0.0;
    for (const MatchPlan& plan : plans) {
        total_work += plan.work;
    }
    const double target_job_work = std::max(
        1000000.0, total_work / static_cast<double>(worker_count * 4)
    );

    std::vector<MatchJob> jobs;
    for (size_t plan_index = 0; plan_index < plans.size(); ++plan_index) {
        const MatchPlan& plan = plans[plan_index];
        const double work_per_row = plan.work / std::max(1, plan.result_h);
        const int rows_per_job = std::max(
            1,
            std::min(
                plan.result_h,
                static_cast<int>(std::ceil(target_job_work / work_per_row))
            )
        );
        for (int y0 = 0; y0 < plan.result_h; y0 += rows_per_job) {
            jobs.push_back(MatchJob{
                plan_index, y0, std::min(plan.result_h, y0 + rows_per_job)
            });
        }
    }
    if (jobs.empty()) {
        return !search_cancelled(cancel_generation);
    }

    const int active_workers = std::min<int>(worker_count, static_cast<int>(jobs.size()));
    std::atomic<size_t> next_job{0};
    std::vector<std::vector<Candidate>> job_results(jobs.size());
    std::exception_ptr worker_error;
    std::mutex error_mutex;
    std::atomic<bool> has_worker_error{false};
    auto worker = [&]() {
        while (true) {
            if (search_cancelled(cancel_generation)
                || has_worker_error.load(std::memory_order_relaxed)) {
                break;
            }
            const size_t index = next_job.fetch_add(1, std::memory_order_relaxed);
            if (index >= jobs.size()) {
                break;
            }
            const MatchJob& job = jobs[index];
            try {
                match_job(
                    plans[job.plan_index],
                    job,
                    threshold,
                    find_all,
                    max_matches,
                    job_results[index],
                    cancel_generation
                );
            } catch (...) {
                std::lock_guard<std::mutex> lock(error_mutex);
                if (!worker_error) {
                    worker_error = std::current_exception();
                }
                has_worker_error.store(true, std::memory_order_relaxed);
                break;
            }
        }
    };

    std::vector<std::thread> threads;
    threads.reserve(static_cast<size_t>(std::max(0, active_workers - 1)));
    try {
        for (int index = 1; index < active_workers; ++index) {
            threads.emplace_back(worker);
        }
    } catch (...) {
        for (std::thread& thread : threads) {
            if (thread.joinable()) {
                thread.join();
            }
        }
        throw;
    }
    worker();
    for (std::thread& thread : threads) {
        thread.join();
    }

    if (worker_error) {
        std::rethrow_exception(worker_error);
    }

    if (search_cancelled(cancel_generation)) {
        return false;
    }

    for (std::vector<Candidate>& result : job_results) {
        out.insert(out.end(), result.begin(), result.end());
    }
    return true;
}

static std::vector<double> build_scales(double min_scale, double max_scale, double scale_step) {
    if (min_scale <= 0.0 || !std::isfinite(min_scale)) {
        min_scale = 1.0;
    }
    if (max_scale <= 0.0 || !std::isfinite(max_scale)) {
        max_scale = 1.0;
    }
    if (max_scale < min_scale) {
        std::swap(min_scale, max_scale);
    }
    if (scale_step <= 0.0 || !std::isfinite(scale_step)) {
        scale_step = 0.05;
    }
    scale_step = std::max(0.01, scale_step);

    std::vector<double> scales;
    scales.push_back(1.0);
    int guard = 0;
    for (double s = min_scale; s <= max_scale + scale_step * 0.25 && guard < 80; s += scale_step, ++guard) {
        if (std::abs(s - 1.0) < 0.01) {
            continue;
        }
        scales.push_back(s);
    }
    return scales;
}

struct TemplateVariant {
    double scale = 1.0;
    PixelImage image;
};

struct TemplateSet {
    std::vector<TemplateVariant> variants;
    size_t bytes = 0;
};

struct TemplateCacheEntry {
    std::wstring key;
    std::shared_ptr<const TemplateSet> templates;
};

static constexpr size_t kTemplateCacheMaxBytes = 256ull * 1024ull * 1024ull;
static constexpr size_t kTemplateCacheMaxEntries = 64;
static constexpr double kWorkBudgetFindAll = 120000000.0;
static constexpr double kWorkBudgetSingle = 180000000.0;
static std::mutex g_template_cache_mutex;
static std::list<TemplateCacheEntry> g_template_cache_lru;
static std::unordered_map<
    std::wstring,
    std::list<TemplateCacheEntry>::iterator
> g_template_cache_index;
static size_t g_template_cache_bytes = 0;

static std::wstring absolute_path(const wchar_t* image_path) {
    if (!image_path) {
        return L"";
    }
    const DWORD required = GetFullPathNameW(image_path, 0, nullptr, nullptr);
    if (required == 0) {
        return std::wstring(image_path);
    }
    std::vector<wchar_t> buffer(static_cast<size_t>(required) + 1, L'\0');
    const DWORD written = GetFullPathNameW(
        image_path, static_cast<DWORD>(buffer.size()), buffer.data(), nullptr
    );
    if (written == 0 || written >= buffer.size()) {
        return std::wstring(image_path);
    }
    return std::wstring(buffer.data(), written);
}

static bool template_cache_key(
    const wchar_t* image_path,
    bool use_gray,
    const std::vector<double>& scales,
    std::wstring& key,
    std::wstring& normalized_path,
    std::wstring& err
) {
    normalized_path = absolute_path(image_path);
    WIN32_FILE_ATTRIBUTE_DATA attributes;
    if (
        normalized_path.empty()
        || !GetFileAttributesExW(
            normalized_path.c_str(), GetFileExInfoStandard, &attributes
        )
    ) {
        err = L"template file attributes unavailable";
        return false;
    }
    const std::uint64_t file_size =
        (static_cast<std::uint64_t>(attributes.nFileSizeHigh) << 32)
        | attributes.nFileSizeLow;
    std::wostringstream stream;
    stream << normalized_path
           << L'|' << attributes.ftLastWriteTime.dwHighDateTime
           << L':' << attributes.ftLastWriteTime.dwLowDateTime
           << L'|' << file_size
           << L'|' << (use_gray ? 1 : 0)
           << std::fixed << std::setprecision(8);
    for (double scale : scales) {
        stream << L'|' << scale;
    }
    key = stream.str();
    return true;
}

static std::shared_ptr<const TemplateSet> get_template_set(
    const wchar_t* image_path,
    bool use_gray,
    const std::vector<double>& scales,
    std::wstring& err,
    std::uint64_t cancel_generation
) {
    if (search_cancelled(cancel_generation)) {
        err = L"native search cancelled";
        return nullptr;
    }
    std::wstring key;
    std::wstring normalized_path;
    if (!template_cache_key(
        image_path, use_gray, scales, key, normalized_path, err
    )) {
        return nullptr;
    }

    {
        std::lock_guard<std::mutex> lock(g_template_cache_mutex);
        const auto found = g_template_cache_index.find(key);
        if (found != g_template_cache_index.end()) {
            g_template_cache_lru.splice(
                g_template_cache_lru.begin(), g_template_cache_lru, found->second
            );
            g_perf_template_cache_hits.fetch_add(1, std::memory_order_relaxed);
            return found->second->templates;
        }
    }

    g_perf_template_cache_misses.fetch_add(1, std::memory_order_relaxed);
    const auto template_started = std::chrono::steady_clock::now();
    Bitmap bitmap(normalized_path.c_str());
    if (
        bitmap.GetLastStatus() != Ok
        || bitmap.GetWidth() <= 0
        || bitmap.GetHeight() <= 0
    ) {
        err = L"template load failed";
        return nullptr;
    }

    auto built = std::make_shared<TemplateSet>();
    built->variants.reserve(scales.size());
    for (double scale : scales) {
        if (search_cancelled(cancel_generation)) {
            err = L"native search cancelled";
            return nullptr;
        }
        PixelImage image;
        if (!bitmap_to_image(bitmap, scale, use_gray, image, err)) {
            continue;
        }
        built->bytes += image.data.size();
        built->variants.push_back(TemplateVariant{scale, std::move(image)});
    }
    g_perf_template_microseconds.fetch_add(
        elapsed_microseconds(template_started), std::memory_order_relaxed
    );
    g_perf_template_variants_built.fetch_add(
        static_cast<std::int64_t>(built->variants.size()),
        std::memory_order_relaxed
    );
    if (built->variants.empty()) {
        if (err.empty()) {
            err = L"template conversion failed";
        }
        return nullptr;
    }
    if (built->bytes > kTemplateCacheMaxBytes) {
        return built;
    }

    std::lock_guard<std::mutex> lock(g_template_cache_mutex);
    const auto existing = g_template_cache_index.find(key);
    if (existing != g_template_cache_index.end()) {
        g_template_cache_lru.splice(
            g_template_cache_lru.begin(), g_template_cache_lru, existing->second
        );
        return existing->second->templates;
    }

    g_template_cache_lru.push_front(TemplateCacheEntry{key, built});
    g_template_cache_index[key] = g_template_cache_lru.begin();
    g_template_cache_bytes += built->bytes;
    while (
        g_template_cache_lru.size() > kTemplateCacheMaxEntries
        || g_template_cache_bytes > kTemplateCacheMaxBytes
    ) {
        const auto last = std::prev(g_template_cache_lru.end());
        g_template_cache_bytes -= last->templates->bytes;
        g_template_cache_index.erase(last->key);
        g_template_cache_lru.erase(last);
    }
    g_perf_cache_bytes.store(
        static_cast<std::int64_t>(g_template_cache_bytes), std::memory_order_relaxed
    );
    g_perf_cache_entries.store(
        static_cast<std::int64_t>(g_template_cache_lru.size()),
        std::memory_order_relaxed
    );
    return built;
}

static std::vector<WrpaRect> build_regions(const WrpaRect* regions, int region_count) {
    std::vector<WrpaRect> out;
    int vx = GetSystemMetrics(SM_XVIRTUALSCREEN);
    int vy = GetSystemMetrics(SM_YVIRTUALSCREEN);
    int vw = GetSystemMetrics(SM_CXVIRTUALSCREEN);
    int vh = GetSystemMetrics(SM_CYVIRTUALSCREEN);
    for (int i = 0; i < region_count; ++i) {
        WrpaRect r = regions[i];
        if (r.w > 0 && r.h > 0) {
            // Clip to virtual screen boundary.
            r.x = (std::max)(r.x, vx);
            r.y = (std::max)(r.y, vy);
            int right = (std::min)(r.x + r.w, vx + vw);
            int bottom = (std::min)(r.y + r.h, vy + vh);
            r.w = right - r.x;
            r.h = bottom - r.y;
            if (r.w <= 0 || r.h <= 0) continue;  // Entirely outside virtual screen.
            out.push_back(r);
        }
    }
    if (!out.empty()) {
        return out;
    }

    WrpaRect virtual_screen;
    virtual_screen.x = GetSystemMetrics(SM_XVIRTUALSCREEN);
    virtual_screen.y = GetSystemMetrics(SM_YVIRTUALSCREEN);
    virtual_screen.w = GetSystemMetrics(SM_CXVIRTUALSCREEN);
    virtual_screen.h = GetSystemMetrics(SM_CYVIRTUALSCREEN);
    if (virtual_screen.w <= 0 || virtual_screen.h <= 0) {
        virtual_screen.x = 0;
        virtual_screen.y = 0;
        virtual_screen.w = GetSystemMetrics(SM_CXSCREEN);
        virtual_screen.h = GetSystemMetrics(SM_CYSCREEN);
    }
    out.push_back(virtual_screen);
    return out;
}

static void sort_and_suppress(std::vector<Candidate>& candidates, bool find_all, int max_matches) {
    std::sort(candidates.begin(), candidates.end(), candidate_better);

    if (!find_all) {
        if (candidates.size() > 1) {
            candidates.resize(1);
        }
        return;
    }

    std::vector<Candidate> accepted;
    accepted.reserve(static_cast<size_t>(max_matches));
    // NMS suppression: O(accepted * candidates) worst case.
    // Bounded by max_matches (at most 4096) and per-job candidate reserve,
    // so actual iterations are far below theoretical maximum.
    for (const Candidate& c : candidates) {
        bool too_close = false;
        for (const Candidate& a : accepted) {
            const double dx = c.x - a.x;
            const double dy = c.y - a.y;
            const double radius = std::max(c.radius, a.radius);
            if (dx * dx + dy * dy <= radius * radius) {
                too_close = true;
                break;
            }
        }
        if (!too_close) {
            accepted.push_back(c);
            if (static_cast<int>(accepted.size()) >= max_matches) {
                break;
            }
        }
    }
    candidates.swap(accepted);
}

extern "C" __declspec(dllexport) int wrpa_version() {
    return 11500;
}

extern "C" __declspec(dllexport) std::uint64_t wrpa_capabilities() {
    return WRPA_CAP_GDI_CAPTURE
        | WRPA_CAP_MULTI_REGION
        | WRPA_CAP_MULTI_SCALE
        | WRPA_CAP_GRAYSCALE
        | WRPA_CAP_COLOR
        | WRPA_CAP_FIND_ALL
        | WRPA_CAP_TEMPLATE_LRU
        | WRPA_CAP_PERF_COUNTERS
        | WRPA_CAP_WORK_BUDGET
        | WRPA_CAP_SINGLE_CAPTURE_PER_REGION
        | WRPA_CAP_ABI_METADATA
        | WRPA_CAP_BOUNDED_JOB_POOL
        | WRPA_CAP_PREFERRED_SCALE_FALLBACK
        | WRPA_CAP_PREFERRED_SCALE_LIST
        | WRPA_CAP_EXPLICIT_SCALE_ONLY
        | WRPA_CAP_LOW_RES_SCENE_FINGERPRINT
        | WRPA_CAP_DXGI_SCENE_CHANGE
        | WRPA_CAP_CANCELLABLE_SEARCH
        | WRPA_CAP_OPENCV_COLOR_SEMANTICS
        | WRPA_CAP_RAW_RGB_CAPTURE;
}

extern "C" __declspec(dllexport) void wrpa_cancel_active_search() {
    g_cancel_generation.fetch_add(1, std::memory_order_relaxed);
}

extern "C" __declspec(dllexport) int wrpa_abi_bits() {
    return static_cast<int>(sizeof(void*) * 8);
}

extern "C" __declspec(dllexport) int wrpa_struct_size(int struct_id) {
    switch (struct_id) {
    case WRPA_STRUCT_RECT:
        return static_cast<int>(sizeof(WrpaRect));
    case WRPA_STRUCT_MATCH:
        return static_cast<int>(sizeof(WrpaMatch));
    case WRPA_STRUCT_PERF_STATS:
        return static_cast<int>(sizeof(WrpaPerfStats));
    default:
        return 0;
    }
}

extern "C" __declspec(dllexport) std::uint64_t wrpa_build_flags() {
    std::uint64_t flags = 0;
#if defined(_WIN64)
    flags |= WRPA_BUILD_X64;
#endif
#if defined(_MT) && !defined(_DLL)
    flags |= WRPA_BUILD_STATIC_CRT;
#endif
#if defined(_MSVC_LANG) && _MSVC_LANG >= 201703L
    flags |= WRPA_BUILD_CPP17;
#endif
#if defined(_WIN32_WINNT) && _WIN32_WINNT >= 0x0A00
    flags |= WRPA_BUILD_WINDOWS10_TARGET;
#endif
#if defined(_MSC_VER)
    flags |= WRPA_BUILD_MSVC;
#endif
    return flags;
}

extern "C" __declspec(dllexport) void wrpa_reset_perf_stats() {
    try {
        g_perf_calls.store(0, std::memory_order_relaxed);
        g_perf_captures.store(0, std::memory_order_relaxed);
        g_perf_integral_builds.store(0, std::memory_order_relaxed);
        g_perf_template_cache_hits.store(0, std::memory_order_relaxed);
        g_perf_template_cache_misses.store(0, std::memory_order_relaxed);
        g_perf_template_variants_built.store(0, std::memory_order_relaxed);
        g_perf_work_budget_fallbacks.store(0, std::memory_order_relaxed);
        g_perf_capture_microseconds.store(0, std::memory_order_relaxed);
        g_perf_template_microseconds.store(0, std::memory_order_relaxed);
        g_perf_match_microseconds.store(0, std::memory_order_relaxed);
        std::lock_guard<std::mutex> lock(g_template_cache_mutex);
        g_perf_cache_bytes.store(
            static_cast<std::int64_t>(g_template_cache_bytes), std::memory_order_relaxed
        );
        g_perf_cache_entries.store(
            static_cast<std::int64_t>(g_template_cache_lru.size()),
            std::memory_order_relaxed
        );
    } catch (const std::bad_alloc&) {
        // silent - reset is best-effort
    } catch (const std::system_error&) {
        // silent
    } catch (...) {
        // silent
    }
}

extern "C" __declspec(dllexport) int wrpa_get_perf_stats(WrpaPerfStats* out_stats) {
    if (!out_stats) {
        return -1;
    }
    out_stats->calls = g_perf_calls.load(std::memory_order_relaxed);
    out_stats->captures = g_perf_captures.load(std::memory_order_relaxed);
    out_stats->integral_builds = g_perf_integral_builds.load(std::memory_order_relaxed);
    out_stats->template_cache_hits = g_perf_template_cache_hits.load(std::memory_order_relaxed);
    out_stats->template_cache_misses = g_perf_template_cache_misses.load(std::memory_order_relaxed);
    out_stats->template_variants_built = g_perf_template_variants_built.load(std::memory_order_relaxed);
    out_stats->work_budget_fallbacks = g_perf_work_budget_fallbacks.load(std::memory_order_relaxed);
    out_stats->capture_microseconds = g_perf_capture_microseconds.load(std::memory_order_relaxed);
    out_stats->template_microseconds = g_perf_template_microseconds.load(std::memory_order_relaxed);
    out_stats->match_microseconds = g_perf_match_microseconds.load(std::memory_order_relaxed);
    out_stats->cache_bytes = g_perf_cache_bytes.load(std::memory_order_relaxed);
    out_stats->cache_entries = g_perf_cache_entries.load(std::memory_order_relaxed);
    return 0;
}

extern "C" __declspec(dllexport) int wrpa_get_virtual_screen_rect(
    WrpaRect* out_rect,
    wchar_t* err_buf,
    int err_len
) {
    if (!out_rect) {
        set_error(err_buf, err_len, L"invalid virtual screen output");
        return -1;
    }
    WrpaRect rect{
        GetSystemMetrics(SM_XVIRTUALSCREEN),
        GetSystemMetrics(SM_YVIRTUALSCREEN),
        GetSystemMetrics(SM_CXVIRTUALSCREEN),
        GetSystemMetrics(SM_CYVIRTUALSCREEN),
    };
    if (rect.w <= 0 || rect.h <= 0) {
        set_error(err_buf, err_len, L"invalid virtual screen dimensions");
        return -1;
    }
    *out_rect = rect;
    return 1;
}

extern "C" __declspec(dllexport) int wrpa_capture_rgb(
    const WrpaRect* region,
    unsigned char* out_pixels,
    int out_capacity,
    wchar_t* err_buf,
    int err_len
) {
    if (!region || !out_pixels || region->w <= 0 || region->h <= 0) {
        set_error(err_buf, err_len, L"invalid RGB capture arguments");
        return -1;
    }
    const std::int64_t required = static_cast<std::int64_t>(region->w)
        * static_cast<std::int64_t>(region->h) * 3;
    if (required <= 0 || required > INT_MAX || out_capacity < required) {
        set_error(err_buf, err_len, L"RGB capture buffer is too small");
        return -1;
    }

    try {
        PixelImage captured;
        std::wstring err;
        if (!capture_rect(*region, false, captured, err)) {
            set_error(err_buf, err_len, err);
            return -1;
        }
        for (std::int64_t index = 0; index < required; index += 3) {
            out_pixels[index + 0] = captured.data[static_cast<size_t>(index + 2)];
            out_pixels[index + 1] = captured.data[static_cast<size_t>(index + 1)];
            out_pixels[index + 2] = captured.data[static_cast<size_t>(index + 0)];
        }
        return 1;
    } catch (const std::bad_alloc&) {
        set_error(err_buf, err_len, L"out of memory");
        return -1;
    } catch (...) {
        set_error(err_buf, err_len, L"internal error");
        return -1;
    }
}

extern "C" __declspec(dllexport) int wrpa_capture_gray_fingerprint(
    const WrpaRect* region,
    int target_w,
    int target_h,
    unsigned char* out_pixels,
    int out_capacity,
    wchar_t* err_buf,
    int err_len
) {
    if (
        !region || !out_pixels
        || target_w <= 0 || target_h <= 0
        || out_capacity < static_cast<std::int64_t>(target_w) * static_cast<std::int64_t>(target_h)
    ) {
        set_error(err_buf, err_len, L"invalid fingerprint arguments");
        return -1;
    }
    try {
        std::wstring err;
        std::vector<unsigned char> fingerprint;
        if (!capture_gray_fingerprint(
            *region, target_w, target_h, fingerprint, err
        )) {
            set_error(err_buf, err_len, err);
            return -1;
        }
        std::copy(fingerprint.begin(), fingerprint.end(), out_pixels);
        return 1;
    } catch (const std::bad_alloc&) {
        set_error(err_buf, err_len, L"out of memory");
        return -1;
    } catch (...) {
        set_error(err_buf, err_len, L"internal error");
        return -1;
    }
}

extern "C" __declspec(dllexport) int wrpa_poll_desktop_change(
    const WrpaRect* regions,
    int region_count,
    int reset_baseline,
    wchar_t* err_buf,
    int err_len
) {
    try {
        std::lock_guard<std::mutex> lock(g_desktop_duplication_mutex);
        std::wstring err;
        const int result = poll_desktop_duplications(
            regions,
            std::max(0, region_count),
            reset_baseline != 0,
            err
        );
        if (result < 0) {
            set_error(err_buf, err_len, err);
        }
        return result;
    } catch (const std::bad_alloc&) {
        set_error(err_buf, err_len, L"out of memory");
        return -1;
    } catch (...) {
        set_error(err_buf, err_len, L"internal error");
        return -1;
    }
}

static int wrpa_find_template_impl(
    const wchar_t* image_path,
    const WrpaRect* regions,
    int region_count,
    double min_scale,
    double max_scale,
    double scale_step,
    int use_gray,
    double threshold,
    int find_all,
    int parallel_mode,
    const double* preferred_scales,
    int preferred_count,
    int explicit_scale_only,
    WrpaMatch* out_matches,
    int max_matches,
    int* out_count,
    wchar_t* err_buf,
    int err_len
) {
    const std::uint64_t cancel_generation = g_cancel_generation.load(
        std::memory_order_relaxed
    );
    g_perf_calls.fetch_add(1, std::memory_order_relaxed);
    if (out_count) {
        *out_count = 0;
    }
    if (!image_path || !out_matches || max_matches <= 0 || !out_count) {
        set_error(err_buf, err_len, L"invalid arguments");
        return -1;
    }

    try {
    std::wstring err;
    if (!ensure_gdiplus(err)) {
        set_error(err_buf, err_len, err);
        return -1;
    }

    threshold = std::max(0.0, std::min(1.0, threshold));
    max_matches = std::max(1, std::min(max_matches, 4096));
    parallel_mode = std::max(0, std::min(parallel_mode, 2));
    preferred_count = preferred_scales
        ? std::max(0, std::min(preferred_count, 16))
        : 0;
    explicit_scale_only = explicit_scale_only != 0 ? 1 : 0;
    if (explicit_scale_only && preferred_count <= 0) {
        return 0;
    }

    std::vector<double> scales = build_scales(min_scale, max_scale, scale_step);
    std::shared_ptr<const TemplateSet> templates = get_template_set(
        image_path, use_gray != 0, scales, err, cancel_generation
    );
    if (!templates) {
        if (search_cancelled(cancel_generation)) {
            set_error(err_buf, err_len, L"native search cancelled");
            return -3;
        }
        set_error(err_buf, err_len, err);
        return -1;
    }
    std::vector<WrpaRect> search_regions = build_regions(regions, region_count);
    std::vector<Candidate> candidates;
    const double budget = find_all ? kWorkBudgetFindAll : kWorkBudgetSingle;
    const auto is_preferred_variant = [&](const TemplateVariant& variant) {
        for (int index = 0; index < preferred_count; ++index) {
            const double preferred = preferred_scales[index];
            if (
                std::isfinite(preferred)
                && preferred > 0.0
                && std::abs(variant.scale - preferred) <= 1e-7
            ) {
                return true;
            }
        }
        return false;
    };
    const auto variant_is_eligible = [&](const TemplateVariant& variant) {
        return !explicit_scale_only || is_preferred_variant(variant);
    };
    const bool use_preferred = preferred_count > 0
        && (find_all == 0 || explicit_scale_only);
    const auto exceeds_budget = [&](double first, double second = 0.0) {
        return !std::isfinite(first)
            || !std::isfinite(second)
            || first > budget
            || second > budget - first;
    };
    const auto reject_for_budget = [&]() {
        g_perf_work_budget_fallbacks.fetch_add(
            1, std::memory_order_relaxed
        );
        set_error(err_buf, err_len, L"native aggregate work budget exceeded");
        return -2;
    };

    // The budget belongs to the complete native call, not an individual
    // scale/region pair. A preferred-scale hit may finish before the remaining
    // plans are needed, so account for that second phase only when it runs.
    double preferred_work = 0.0;
    double remaining_work = 0.0;
    for (const WrpaRect& region : search_regions) {
        for (const TemplateVariant& variant : templates->variants) {
            if (!variant_is_eligible(variant)) {
                continue;
            }
            const PixelImage& templ = variant.image;
            if (templ.w > region.w || templ.h > region.h) {
                continue;
            }
            const int rw = region.w - templ.w + 1;
            const int rh = region.h - templ.h + 1;
            const double work = static_cast<double>(rw) * rh
                * templ.w * templ.h * templ.channels;
            if (use_preferred && is_preferred_variant(variant)) {
                preferred_work += work;
            } else {
                remaining_work += work;
            }
        }
    }
    if (exceeds_budget(preferred_work)) {
        return reject_for_budget();
    }
    if (!use_preferred && exceeds_budget(remaining_work)) {
        return reject_for_budget();
    }
    if (use_preferred && preferred_work <= 0.0 && exceeds_budget(remaining_work)) {
        return reject_for_budget();
    }

    std::vector<CapturedRegion> captured_regions;
    captured_regions.reserve(search_regions.size());
    for (const WrpaRect& region : search_regions) {
        if (search_cancelled(cancel_generation)) {
            set_error(err_buf, err_len, L"native search cancelled");
            return -3;
        }
        bool needs_capture = false;
        for (const TemplateVariant& variant : templates->variants) {
            if (!variant_is_eligible(variant)) {
                continue;
            }
            const PixelImage& templ = variant.image;
            if (templ.w > region.w || templ.h > region.h) {
                continue;
            }
            needs_capture = true;
            break;
        }
        if (!needs_capture) {
            continue;
        }

        CapturedRegion captured;
        captured.rect = region;
        const auto capture_started = std::chrono::steady_clock::now();
        g_perf_captures.fetch_add(1, std::memory_order_relaxed);
        const bool captured_ok = capture_rect(
            region, use_gray != 0, captured.screen, err
        );
        g_perf_capture_microseconds.fetch_add(
            elapsed_microseconds(capture_started), std::memory_order_relaxed
        );
        if (!captured_ok) {
            set_error(
                err_buf,
                err_len,
                err.empty() ? L"native screen capture failed" : err
            );
            return -1;
        }

        if (search_cancelled(cancel_generation)) {
            set_error(err_buf, err_len, L"native search cancelled");
            return -3;
        }

        const auto integral_started = std::chrono::steady_clock::now();
        build_integral(captured.screen, captured.sum, captured.sqsum);
        g_perf_integral_builds.fetch_add(1, std::memory_order_relaxed);
        g_perf_match_microseconds.fetch_add(
            elapsed_microseconds(integral_started), std::memory_order_relaxed
        );
        captured_regions.push_back(std::move(captured));
    }

    std::vector<MatchPlan> preferred_plans;
    std::vector<MatchPlan> remaining_plans;
    for (const TemplateVariant& variant : templates->variants) {
        const bool preferred_variant = use_preferred
            && is_preferred_variant(variant);
        if (explicit_scale_only && !preferred_variant) {
            continue;
        }
        for (const CapturedRegion& captured : captured_regions) {
            MatchPlan plan;
            if (!build_match_plan(captured, variant.image, variant.scale, plan)) {
                continue;
            }
            if (preferred_variant) {
                preferred_plans.push_back(plan);
            } else {
                remaining_plans.push_back(plan);
            }
        }
    }

    const auto match_started = std::chrono::steady_clock::now();
    if (!preferred_plans.empty()) {
        if (!run_match_plans(
            preferred_plans,
            parallel_mode,
            threshold,
            false,
            max_matches,
            candidates,
            cancel_generation
        )) {
            set_error(err_buf, err_len, L"native search cancelled");
            return -3;
        }
    }
    if (candidates.empty() && !explicit_scale_only) {
        if (exceeds_budget(preferred_work, remaining_work)) {
            g_perf_match_microseconds.fetch_add(
                elapsed_microseconds(match_started), std::memory_order_relaxed
            );
            return reject_for_budget();
        }
        if (!run_match_plans(
            remaining_plans,
            parallel_mode,
            threshold,
            find_all != 0,
            max_matches,
            candidates,
            cancel_generation
        )) {
            set_error(err_buf, err_len, L"native search cancelled");
            return -3;
        }
    }
    g_perf_match_microseconds.fetch_add(
        elapsed_microseconds(match_started), std::memory_order_relaxed
    );

    if (candidates.empty()) {
        return 0;
    }

    sort_and_suppress(candidates, find_all != 0, max_matches);
    const int count = std::min<int>(static_cast<int>(candidates.size()), max_matches);
    for (int i = 0; i < count; ++i) {
        out_matches[i].x = candidates[static_cast<size_t>(i)].x;
        out_matches[i].y = candidates[static_cast<size_t>(i)].y;
        out_matches[i].scale = candidates[static_cast<size_t>(i)].scale;
        out_matches[i].score = candidates[static_cast<size_t>(i)].score;
        out_matches[i].radius = candidates[static_cast<size_t>(i)].radius;
    }
    *out_count = count;
    return count > 0 ? 1 : 0;
    } catch (const std::bad_alloc&) {
        set_error(err_buf, err_len, L"out of memory");
        return -1;
    } catch (const std::system_error&) {
        set_error(err_buf, err_len, L"system error (thread or resource failure)");
        return -1;
    } catch (...) {
        set_error(err_buf, err_len, L"internal error");
        return -1;
    }
}

extern "C" __declspec(dllexport) int wrpa_find_template(
    const wchar_t* image_path,
    const WrpaRect* regions,
    int region_count,
    double min_scale,
    double max_scale,
    double scale_step,
    int use_gray,
    double threshold,
    int find_all,
    WrpaMatch* out_matches,
    int max_matches,
    int* out_count,
    wchar_t* err_buf,
    int err_len
) {
    return wrpa_find_template_impl(
        image_path,
        regions,
        region_count,
        min_scale,
        max_scale,
        scale_step,
        use_gray,
        threshold,
        find_all,
        1,
        nullptr,
        0,
        0,
        out_matches,
        max_matches,
        out_count,
        err_buf,
        err_len
    );
}

extern "C" __declspec(dllexport) int wrpa_find_template_ex(
    const wchar_t* image_path,
    const WrpaRect* regions,
    int region_count,
    double min_scale,
    double max_scale,
    double scale_step,
    int use_gray,
    double threshold,
    int find_all,
    int parallel_mode,
    double preferred_scale,
    WrpaMatch* out_matches,
    int max_matches,
    int* out_count,
    wchar_t* err_buf,
    int err_len
) {
    const double* preferred_ptr = (
        std::isfinite(preferred_scale) && preferred_scale > 0.0
    ) ? &preferred_scale : nullptr;
    return wrpa_find_template_impl(
        image_path,
        regions,
        region_count,
        min_scale,
        max_scale,
        scale_step,
        use_gray,
        threshold,
        find_all,
        parallel_mode,
        preferred_ptr,
        preferred_ptr ? 1 : 0,
        0,
        out_matches,
        max_matches,
        out_count,
        err_buf,
        err_len
    );
}

extern "C" __declspec(dllexport) int wrpa_find_template_ex2(
    const wchar_t* image_path,
    const WrpaRect* regions,
    int region_count,
    double min_scale,
    double max_scale,
    double scale_step,
    int use_gray,
    double threshold,
    int find_all,
    int parallel_mode,
    const double* preferred_scales,
    int preferred_count,
    WrpaMatch* out_matches,
    int max_matches,
    int* out_count,
    wchar_t* err_buf,
    int err_len
) {
    return wrpa_find_template_impl(
        image_path,
        regions,
        region_count,
        min_scale,
        max_scale,
        scale_step,
        use_gray,
        threshold,
        find_all,
        parallel_mode,
        preferred_scales,
        preferred_count,
        0,
        out_matches,
        max_matches,
        out_count,
        err_buf,
        err_len
    );
}

extern "C" __declspec(dllexport) int wrpa_find_template_ex3(
    const wchar_t* image_path,
    const WrpaRect* regions,
    int region_count,
    double min_scale,
    double max_scale,
    double scale_step,
    int use_gray,
    double threshold,
    int find_all,
    int parallel_mode,
    const double* preferred_scales,
    int preferred_count,
    int explicit_scale_only,
    WrpaMatch* out_matches,
    int max_matches,
    int* out_count,
    wchar_t* err_buf,
    int err_len
) {
    return wrpa_find_template_impl(
        image_path,
        regions,
        region_count,
        min_scale,
        max_scale,
        scale_step,
        use_gray,
        threshold,
        find_all,
        parallel_mode,
        preferred_scales,
        preferred_count,
        explicit_scale_only,
        out_matches,
        max_matches,
        out_count,
        err_buf,
        err_len
    );
}

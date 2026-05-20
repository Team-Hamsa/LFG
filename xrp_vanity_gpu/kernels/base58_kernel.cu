// Base58Check encoding and pattern matching device functions for XRP vanity address generation
// No includes, no global kernels - concatenate into your kernel.
// XRP Base58 alphabet: rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz

__device__ static const char XRP_B58_ALPHA[59] =
    "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz";

// ---------------------------------------------------------------------------
// base58_encode_n
//   Generic base58 encoder for fixed-size payloads.
//   payload   : input bytes (big-endian)
//   n         : number of input bytes
//   out       : output buffer (null-terminated); caller must provide enough space
//   max_digits: scratch-array size (use 40 for 25-byte input, 36 for 23-byte)
// ---------------------------------------------------------------------------
__device__ static void base58_encode_n(const uint8_t *payload, int n,
                                        char *out, int max_digits)
{
    // digits[] holds the base-58 representation, least-significant first.
    // We accumulate by treating each input byte as multiplying the current
    // number by 256 and adding the byte (big-endian stream ? big integer).
    uint8_t digits[40]; // 40 is enough for up to 29 input bytes
    int     ndigits = 0;

    // Initialise to zero
    for (int i = 0; i < max_digits; i++) digits[i] = 0;

    for (int i = 0; i < n; i++) {
        uint32_t carry = (uint32_t)payload[i];
        for (int j = 0; j < ndigits; j++) {
            carry += (uint32_t)digits[j] * 256u;
            digits[j] = (uint8_t)(carry % 58u);
            carry /= 58u;
        }
        while (carry > 0) {
            digits[ndigits++] = (uint8_t)(carry % 58u);
            carry /= 58u;
        }
    }

    // Count leading zero bytes in payload ? leading 'r' characters
    int leading_zeros = 0;
    for (int i = 0; i < n; i++) {
        if (payload[i] == 0x00) leading_zeros++;
        else break;
    }

    // Write leading 'r' chars (index 0 in XRP alphabet = 'r')
    int pos = 0;
    for (int i = 0; i < leading_zeros; i++) {
        out[pos++] = 'r';
    }

    // Append digits in most-significant-first order (reverse of digits[])
    for (int i = ndigits - 1; i >= 0; i--) {
        out[pos++] = XRP_B58_ALPHA[(int)digits[i]];
    }

    out[pos] = '\0';
}

// ---------------------------------------------------------------------------
// base58_encode_address
//   Encode a 25-byte XRP classic address payload.
//   payload = [0x00] + 20-byte account_id + 4-byte checksum
//   Output: null-terminated string, max 35 chars + NUL
// ---------------------------------------------------------------------------
__device__ void base58_encode_address(const uint8_t *payload, char *out)
{
    base58_encode_n(payload, 25, out, 35);
}

// ---------------------------------------------------------------------------
// base58_encode_seed
//   Encode a 23-byte XRP Ed25519 seed payload.
//   payload23 = [0x01, 0xE1, 0x4B] + 16-byte raw seed + 4-byte checksum
//   Output: null-terminated string, max 30 chars + NUL
// ---------------------------------------------------------------------------
__device__ void base58_encode_seed(const uint8_t *payload23, char *out)
{
    base58_encode_n(payload23, 23, out, 33);
}

// ---------------------------------------------------------------------------
// contains_icase
//   Case-insensitive substring search.
//   Returns 1 if pattern (of length pattern_len) is found anywhere in address.
//   address must be null-terminated; pattern need not be.
// ---------------------------------------------------------------------------
__device__ static char b58_to_lower(char c)
{
    if (c >= 'A' && c <= 'Z') return (char)(c + 32);
    return c;
}

__device__ int contains_icase(const char *address, const char *pattern, int pattern_len)
{
    if (pattern_len == 0) return 1;

    // Find length of address
    int addr_len = 0;
    while (address[addr_len] != '\0') addr_len++;

    if (addr_len < pattern_len) return 0;

    int limit = addr_len - pattern_len;
    for (int i = 0; i <= limit; i++) {
        int match = 1;
        for (int j = 0; j < pattern_len; j++) {
            if (b58_to_lower(address[i + j]) != b58_to_lower(pattern[j])) {
                match = 0;
                break;
            }
        }
        if (match) return 1;
    }
    return 0;
}

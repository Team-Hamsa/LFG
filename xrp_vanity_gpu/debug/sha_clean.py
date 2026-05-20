import hashlib, struct
K=[0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
   0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
   0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
   0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
   0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
   0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
   0x19a4c116,0x1e376c08,0x27487740,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
   0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]

def rotr(x,n): return ((x>>n)|(x<<(32-n)))&0xFFFFFFFF

def compress(state, W):
    a,b,c,d,e,f,g,h = state
    for i in range(64):
        S1 = rotr(e,6)^rotr(e,11)^rotr(e,25)
        ch = (e&f)^((~e&0xFFFFFFFF)&g)
        T1 = (h+S1+ch+K[i]+W[i])&0xFFFFFFFF
        S0 = rotr(a,2)^rotr(a,13)^rotr(a,22)
        mj = (a&b)^(a&c)^(b&c)
        T2 = (S0+mj)&0xFFFFFFFF
        new_a = (T1+T2)&0xFFFFFFFF
        new_e = (d+T1)&0xFFFFFFFF
        h = g; g = f; f = e; e = new_e
        d = c; c = b; b = a; a = new_a
    return [(s+v)&0xFFFFFFFF for s,v in zip(state,[a,b,c,d,e,f,g,h])]

def sha256(msg):
    L = len(msg)*8
    msg = msg + b'\x80'
    while len(msg)%64 != 56: msg += b'\x00'
    msg += L.to_bytes(8,'big')
    H = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]
    for off in range(0,len(msg),64):
        block = msg[off:off+64]
        W = list(struct.unpack('>16I', block))
        for i in range(16,64):
            s0 = rotr(W[i-15],7)^rotr(W[i-15],18)^(W[i-15]>>3)
            s1 = rotr(W[i-2],17)^rotr(W[i-2],19)^(W[i-2]>>10)
            W.append((W[i-16]+s0+W[i-7]+s1)&0xFFFFFFFF)
        H = compress(H, W)
    return b''.join(struct.pack('>I',h) for h in H).hex()

print('mine:', sha256(b'abc'))
print('std: ', hashlib.sha256(b'abc').hexdigest())

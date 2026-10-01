// The engine's real-browser implementations: hashing and uploading.

export async function sha256Hex(file: File) {
  // Web Crypto has no streaming hash, so the whole file is read into memory once.
  // Fine for photos; it needs a secure context (HTTPS or localhost).
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

// fetch() can't report upload progress, so the PUT uses XMLHttpRequest.
export function putWithProgress(url: string, file: File, contentType: string, onProgress: (fraction: number) => void) {
  return new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('PUT', url)
    // The signed URL covers the content type, so it must match what was declared.
    xhr.setRequestHeader('Content-Type', contentType)
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total)
    xhr.onload = () => (xhr.status < 300 ? resolve() : reject(new Error(`storage rejected the upload (${xhr.status})`)))
    xhr.onerror = () => reject(new Error('network error during upload'))
    xhr.send(file)
  })
}

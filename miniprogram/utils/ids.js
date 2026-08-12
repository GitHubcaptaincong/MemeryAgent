function randomHex(length) {
  let value = ''
  const alphabet = '0123456789abcdef'
  for (let index = 0; index < length; index += 1) {
    value += alphabet[Math.floor(Math.random() * alphabet.length)]
  }
  return value
}

function uuid() {
  return `${randomHex(8)}-${randomHex(4)}-4${randomHex(3)}-a${randomHex(3)}-${randomHex(12)}`
}

function idempotencyKey(prefix) {
  return `${prefix}-${Date.now()}-${randomHex(10)}`
}

module.exports = { uuid, idempotencyKey }

const creationTimeFormatter = new Intl.DateTimeFormat('en-IN', {
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: true,
  timeZoneName: 'short',
})

export function formatCreatedAt(timestamp: string): string {
  const date = new Date(timestamp)
  return Number.isNaN(date.getTime()) ? 'Unknown' : creationTimeFormatter.format(date)
}

import { useEffect, useRef } from 'react'
import Plotly from 'plotly.js/lib/core'
import scatter from 'plotly.js/lib/scatter'
import type { Series } from './api'

Plotly.register([scatter])

export function Plot({ page, sensors }: { page: Series; sensors: string[] }) {
  const host = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!host.current) return
    const traces = sensors.map((sensor, index) => ({
      type: 'scatter' as const, mode: 'lines' as const, name: `${sensor} (${page.sensor_units[sensor] ?? 'unit unknown'})`,
      x: page.items.map(row => row.event_time_source),
      y: page.items.map(row => row.measurements[sensor]),
      line: { color: ['#51e0d4', '#e7b96b', '#94a7ff', '#f08b8b'][index], width: 2 },
      hovertemplate: '%{x}<br>%{y:.3f}<extra>%{fullData.name}</extra>',
    }))
    void Plotly.react(host.current, traces, {
      paper_bgcolor: 'transparent', plot_bgcolor: 'transparent', font: { color: '#9fb7c3', family: 'Inter, system-ui, sans-serif' },
      xaxis: { title: { text: 'Source time (timezone unknown)' }, gridcolor: '#24343f', type: 'date' },
      yaxis: { title: { text: sensors.length === 1 ? page.sensor_units[sensors[0]] ?? 'Value' : 'Sensor values (mixed units)' }, gridcolor: '#24343f' },
      margin: { l: 70, r: 24, t: 15, b: 70 }, showlegend: true, legend: { orientation: 'h', y: 1.12 },
      hovermode: 'x unified', autosize: true,
    }, { responsive: true, displaylogo: false, modeBarButtonsToRemove: ['toImage', 'lasso2d', 'select2d'] })
    return () => { if (host.current) Plotly.purge(host.current) }
  }, [page, sensors])
  return <div ref={host} className="plot" role="img" aria-label={`Sensor trend chart showing ${page.items.length} rows on this page. Use the data table for accessible values.`} />
}

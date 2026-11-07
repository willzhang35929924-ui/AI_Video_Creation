export type Resolution = {width:number;height:number};

function resolution(value:unknown):Resolution|undefined {
  if(!value || typeof value!=='object')return;
  const {width,height}=value as Partial<Resolution>;
  if(typeof width==='number'&&typeof height==='number'&&width>0&&height>0)return {width,height};
}

export function videoResolution(metadata:unknown):Resolution|undefined {
  if(!metadata || typeof metadata!=='object')return;
  const value=metadata as {quality?:{final_actual?:unknown};actual?:unknown};
  return resolution(value.quality?.final_actual)||resolution(value.actual)||resolution(metadata);
}

export function sourceCanvas(sources:(Resolution|undefined)[]):Resolution|undefined {
  if(!sources.length||sources.some(source=>!source))return;
  const dimensions=sources as Resolution[];
  return {
    width:Math.max(128,Math.ceil(Math.max(...dimensions.map(source=>source.width))/2)*2),
    height:Math.max(128,Math.ceil(Math.max(...dimensions.map(source=>source.height))/2)*2),
  };
}

export function reducesResolution(output:Resolution,sources:(Resolution|undefined)[]) {
  return sources.some(source=>source&&(output.width<source.width||output.height<source.height));
}

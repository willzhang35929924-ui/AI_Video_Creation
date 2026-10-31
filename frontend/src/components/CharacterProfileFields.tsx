import { FormField } from './ui';
export type IdentityProfile = { age: number | null; original_adult?: boolean | null; description: string; facial_features: string; hair: string; eyes: string; skin: string; identifying_marks: string; body: string; style: string; fixed_traits: string[]; variable_attributes: string[]; scene_constraints: string[]; wardrobes: { id: string; description: string; label?: string }[] };
export const emptyProfile = (): IdentityProfile => ({ age: null, original_adult: null, description: '', facial_features: '', hair: '', eyes: '', skin: '', identifying_marks: '', body: '', style: 'Photorealistic cinematic photography, natural skin texture.', fixed_traits: [], variable_attributes: ['expression', 'pose', 'lighting', 'scene'], scene_constraints: [], wardrobes: [{ id: 'default', label: '默认服装', description: '' }] });
const profileRecord=(value:unknown):Record<string,unknown>|null=>value!==null&&typeof value==='object'&&!Array.isArray(value)?value as Record<string,unknown>:null;
function legacyProfile(source:Record<string,unknown>,fallbackDescription=''):Partial<IdentityProfile>{
  const profile:Partial<IdentityProfile>={};
  const text=(value:unknown)=>typeof value==='string'?value:Array.isArray(value)&&value.every(item=>typeof item==='string')?value.join('；'):'';
  for(const key of ['description','eyes','skin','style','identifying_marks'] as const){if(typeof source[key]==='string')profile[key]=source[key];}
  profile.facial_features=['facial_features','eyebrows','nose','lips','jaw'].map(key=>text(source[key])).filter(Boolean).join('；');
  profile.body=['body','shoulders','hands'].map(key=>text(source[key])).filter(Boolean).join('；');
  profile.hair=['hair','hairline'].map(key=>text(source[key])).filter(Boolean).join('；');
  if(!profile.identifying_marks&&source.marks)profile.identifying_marks=text(source.marks);
  for(const key of ['fixed_traits','variable_attributes','scene_constraints'] as const){
    if(Array.isArray(source[key]))profile[key]=(source[key] as unknown[]).filter((value):value is string=>typeof value==='string');
    else if(typeof source[key]==='string')profile[key]=source[key].split(/[；;\n]/).map(value=>value.trim()).filter(Boolean);
  }
  if(!profile.fixed_traits?.length&&source.immutable_traits)profile.fixed_traits=text(source.immutable_traits).split(/[；;\n]/).map(value=>value.trim()).filter(Boolean);
  if(Array.isArray(source.wardrobes))profile.wardrobes=source.wardrobes as IdentityProfile['wardrobes'];
  if(!profile.wardrobes?.length&&source.wardrobe)profile.wardrobes=[{id:'default',label:'默认服装',description:text(source.wardrobe)}];
  if(typeof source.original_adult==='boolean')profile.original_adult=source.original_adult;
  profile.description=profile.description||fallbackDescription;
  const age=source.age;
  const parsed=typeof age==='string'&&/^\s*\d+\s*(?:岁|years? old)?\s*$/i.test(age)?Number(age.match(/\d+/)![0]):age;
  profile.age=typeof parsed==='number'&&Number.isSafeInteger(parsed)&&parsed>=0?parsed:null;
  if(profile.age===null&&age!==undefined&&age!==null&&age!=='')profile.description=(profile.description||'')+'；原文年龄描述：'+String(age);
  return profile;
}
export function readProfile(identity: Record<string, unknown>): IdentityProfile {
  const defaults={...emptyProfile(),description:String(identity.fixed_identity||identity.fixedIdentity||''),facial_features:String(identity.facial_features||identity.facialFeatures||''),hair:String(identity.hairstyle||''),wardrobes:[{id:'default',label:'默认服装',description:String(identity.default_clothing||identity.defaultOutfit||'')}]};
  const saved=profileRecord(identity.profile);
  const source=profileRecord(identity.source_profile);
  const raw:Partial<IdentityProfile>=saved&&Object.keys(saved).length?saved as Partial<IdentityProfile>:source&&Object.keys(source).length?legacyProfile(source,defaults.description):{};
  return {...defaults,...raw,age:typeof raw.age==='number'&&Number.isFinite(raw.age)?raw.age:null,
    description:raw.description??defaults.description,fixed_traits:raw.fixed_traits??defaults.fixed_traits,
    variable_attributes:raw.variable_attributes??defaults.variable_attributes,scene_constraints:raw.scene_constraints??defaults.scene_constraints,
    wardrobes:raw.wardrobes??defaults.wardrobes};
}
export function CharacterProfileFields({ value, onChange, fixedLocked = false, allLocked = false }: { value: IdentityProfile; onChange: (value: IdentityProfile) => void; fixedLocked?: boolean; allLocked?: boolean }) {
  const set = (key: string, next: unknown) => onChange({ ...value, [key]: next });
  return <div className="editor-form-grid identity-fields">
    <FormField label="年龄（可选）" hint="未填写时保留未知，不会自动补入年龄。"><input type="number" min={0} step={1} disabled={allLocked || fixedLocked} value={value.age??''} onChange={event => set('age', event.target.value===''?null:Number(event.target.value))}/></FormField>
    <div className="form-span-2"><FormField label="人物描述" hint="可选；也可使用剧本已有描述或人物参考图。"><textarea rows={3} disabled={allLocked || fixedLocked} value={value.description} onChange={event => set('description', event.target.value)}/></FormField></div>
    {([['facial_features', '面部特征'], ['hair', '发型与发色'], ['eyes', '眼睛特征'], ['skin', '肤色描述'], ['identifying_marks', '可识别标记'], ['body', '身材特征']] as const).map(([key, label]) => <FormField key={key} label={label}><input disabled={allLocked || fixedLocked} value={value[key]} onChange={event => set(key, event.target.value)}/></FormField>)}
    <div className="form-span-2"><FormField label="禁止变化的身份特征" hint="每行一项；批准标准脸后固定特征锁定"><textarea rows={2} disabled={allLocked || fixedLocked} value={value.fixed_traits.join('\n')} onChange={event => set('fixed_traits', event.target.value.split('\n'))}/></FormField></div>
    <FormField label="可变化属性"><textarea rows={2} disabled={allLocked} value={value.variable_attributes.join('\n')} onChange={event => set('variable_attributes', event.target.value.split('\n'))}/></FormField>
    <FormField label="场景约束"><textarea rows={2} disabled={allLocked} value={value.scene_constraints.join('\n')} onChange={event => set('scene_constraints', event.target.value.split('\n'))}/></FormField>
    <FormField label="默认服装（独立于身份）"><textarea rows={2} disabled={allLocked} value={value.wardrobes.find(item => item.id === 'default')?.description || ''} onChange={event => set('wardrobes', [{ id: 'default', label: '默认服装', description: event.target.value }, ...value.wardrobes.filter(item => item.id !== 'default')])}/></FormField>
    <FormField label="风格约束"><textarea rows={2} disabled={allLocked} value={value.style} onChange={event => set('style', event.target.value)}/></FormField>
  </div>;
}

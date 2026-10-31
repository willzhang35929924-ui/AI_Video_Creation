"""Resolve optional character facts without inventing an adult age or identity."""
import re
from app.schemas_product import IdentityProfile


def source_profile(raw, name='', description=''):
    raw=raw if isinstance(raw,dict) else {}
    age=raw.get('age')
    # Source documents often contain an age label instead of an exact integer.
    # Preserve that label in the description; never replace it with a fake age.
    if isinstance(age,str) and re.fullmatch(r'\s*\d+\s*(?:岁|years? old)?\s*',age,re.I):
        age=int(re.search(r'\d+',age).group())
    age_note='' if age in (None,'') or isinstance(age,int) and not isinstance(age,bool) and age>=0 else str(age)
    if isinstance(age,bool) or not isinstance(age,int) or age<0:age=None
    facts={key:raw[key] for key in IdentityProfile.model_fields if key in raw and key!='age'}
    facts['description']=raw.get('description') or description or name
    if age_note:facts['description']+='；原文年龄描述：'+age_note
    facts['facial_features']='；'.join(str(raw[key]) for key in ('facial_features','eyebrows','nose','lips','jaw') if raw.get(key))
    facts['body']='；'.join(str(raw[key]) for key in ('body','shoulders','hands') if raw.get(key))
    facts['hair']='；'.join(str(raw[key]) for key in ('hair','hairline') if raw.get(key))
    if raw.get('marks') and not facts.get('identifying_marks'):facts['identifying_marks']=raw['marks']
    if raw.get('immutable_traits') and not facts.get('fixed_traits'):
        facts['fixed_traits']=[value.strip() for value in re.split('[；;\n]',str(raw['immutable_traits'])) if value.strip()]
    if raw.get('wardrobe') and not facts.get('wardrobes'):
        facts['wardrobes']=[{'id':'default','label':'默认服装','description':raw['wardrobe']}]
    return IdentityProfile(age=age,**facts)


def resolve_profile(identity, *, name='', description=''):
    identity=identity if isinstance(identity,dict) else {}
    raw=identity.get('profile')
    if not isinstance(raw,dict) or not raw:
        return source_profile(identity.get('source_profile') or {},name,description or identity.get('fixed_identity',''))
    return source_profile(raw,name,description)


def age_description(profile):
    return f', {profile.age} years old' if profile.age is not None else ''
